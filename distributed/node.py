import asyncio
import logging
import warnings
import weakref

from tornado import gen
from tornado.ioloop import IOLoop
from tornado.httpserver import HTTPServer
import tlz
import dask

from .comm import get_tcp_server_address
from .comm import get_address_host
from .core import Server, ConnectionPool
from .http.routing import RoutingApplication
from .versions import get_versions
from .utils import DequeHandler, TimeoutError, clean_dashboard_address, ignoring


class Node:
    """
    Base class for nodes in a distributed cluster.
    """

    def __init__(
        self,
        connection_limit=512,
        deserialize=True,
        connection_args=None,
        io_loop=None,
        serializers=None,
        deserializers=None,
        timeout=None,
    ):
        self.io_loop = io_loop or IOLoop.current()
        self.rpc = ConnectionPool(
            limit=connection_limit,
            deserialize=deserialize,
            serializers=serializers,
            deserializers=deserializers,
            connection_args=connection_args,
            timeout=timeout,
            server=self,
        )

    async def start(self):
        await self.rpc.start()


class ServerNode(Node, Server):
    """
    Base class for server nodes in a distributed cluster.
    """

    # TODO factor out security, listening, services, etc. here

    # XXX avoid inheriting from Server? there is some large potential for confusion
    # between base and derived attribute namespaces...

    def __init__(
        self,
        handlers=None,
        blocked_handlers=None,
        stream_handlers=None,
        connection_limit=512,
        deserialize=True,
        connection_args=None,
        io_loop=None,
        serializers=None,
        deserializers=None,
        timeout=None,
    ):
        Node.__init__(
            self,
            deserialize=deserialize,
            connection_limit=connection_limit,
            connection_args=connection_args,
            io_loop=io_loop,
            serializers=serializers,
            deserializers=deserializers,
            timeout=timeout,
        )
        Server.__init__(
            self,
            handlers=handlers,
            blocked_handlers=blocked_handlers,
            stream_handlers=stream_handlers,
            connection_limit=connection_limit,
            deserialize=deserialize,
            io_loop=self.io_loop,
        )

    def versions(self, comm=None, packages=None):
        return get_versions(packages=packages)

    def start_services(self, default_listen_ip):
        if default_listen_ip == "0.0.0.0":
            default_listen_ip = ""  # for IPV6

        for k, v in self.service_specs.items():
            listen_ip = None
            if isinstance(k, tuple):
                k, port = k
            else:
                port = 0

            if isinstance(port, str):
                port = port.split(":")

            if isinstance(port, (tuple, list)):
                if len(port) == 2:
                    listen_ip, port = (port[0], int(port[1]))
                elif len(port) == 1:
                    [listen_ip], port = port, 0
                else:
                    raise ValueError(port)

            if isinstance(v, tuple):
                v, kwargs = v
            else:
                kwargs = {}

            try:
                service = v(self, io_loop=self.loop, **kwargs)
                service.listen(
                    (listen_ip if listen_ip is not None else default_listen_ip, port)
                )
                self.services[k] = service
            except Exception as e:
                warnings.warn(
                    "\nCould not launch service '%s' on port %s. " % (k, port)
                    + "Got the following message:\n\n"
                    + str(e),
                    stacklevel=3,
                )

    def stop_services(self):
        for service in self.services.values():
            service.stop()

    @property
    def service_ports(self):
        return {k: v.port for k, v in self.services.items()}

    def _setup_logging(self, logger):
        self._deque_handler = DequeHandler(
            n=dask.config.get("distributed.admin.log-length")
        )
        self._deque_handler.setFormatter(
            logging.Formatter(dask.config.get("distributed.admin.log-format"))
        )
        logger.addHandler(self._deque_handler)
        weakref.finalize(self, logger.removeHandler, self._deque_handler)

    def get_logs(self, comm=None, n=None):
        deque_handler = self._deque_handler
        if n is None:
            L = list(deque_handler.deque)
        else:
            L = deque_handler.deque
            L = [L[-i] for i in range(min(n, len(L)))]
        return [(msg.levelname, deque_handler.format(msg)) for msg in L]

    async def __aenter__(self):
        await self
        return self

    async def __aexit__(self, typ, value, traceback):
        await self.close()

    def __await__(self):
        if self.status == "running":
            return gen.sleep(0).__await__()
        else:
            future = self.start()
            timeout = getattr(self, "death_timeout", 0)
            if timeout:

                async def wait_for(future, timeout=None):
                    try:
                        await asyncio.wait_for(future, timeout=timeout)
                    except Exception:
                        await self.close(timeout=1)
                        raise TimeoutError(
                            "{} failed to start in {} seconds".format(
                                type(self).__name__, timeout
                            )
                        )

                future = wait_for(future, timeout=timeout)
            return future.__await__()

    async def start(self):
        # subclasses should implement their own start method whichs calls super().start()
        await Node.start(self)
        return self

    def start_http_server(
        self, routes, dashboard_address, default_port=0, ssl_options=None,
    ):
        """ This creates an HTTP Server running on this node """

        self.http_application = RoutingApplication(routes,)

        # TLS configuration
        tls_key = dask.config.get("distributed.scheduler.dashboard.tls.key")
        tls_cert = dask.config.get("distributed.scheduler.dashboard.tls.cert")
        tls_ca_file = dask.config.get("distributed.scheduler.dashboard.tls.ca-file")
        if tls_cert:
            import ssl

            ssl_options = ssl.create_default_context(
                cafile=tls_ca_file, purpose=ssl.Purpose.SERVER_AUTH
            )
            ssl_options.load_cert_chain(tls_cert, keyfile=tls_key)
            # We don't care about auth here, just encryption
            ssl_options.check_hostname = False
            ssl_options.verify_mode = ssl.CERT_NONE

        self.http_server = HTTPServer(self.http_application, ssl_options=ssl_options)
        http_address = clean_dashboard_address(dashboard_address or default_port)

        if not http_address["address"]:
            address = self._start_address
            if isinstance(address, (list, tuple)):
                address = address[0]
            if address:
                with ignoring(ValueError):
                    http_address["address"] = get_address_host(address)
        changed_port = False
        try:
            self.http_server.listen(**http_address)
        except Exception:
            changed_port = True
            self.http_server.listen(**tlz.merge(http_address, {"port": 0}))
        self.http_server.port = get_tcp_server_address(self.http_server)[1]
        self.services["dashboard"] = self.http_server

        if changed_port and dashboard_address:
            warnings.warn(
                "Port {} is already in use.\n"
                "Perhaps you already have a cluster running?\n"
                "Hosting the HTTP server on port {} instead".format(
                    http_address["port"], self.http_server.port
                )
            )
