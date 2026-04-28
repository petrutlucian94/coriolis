# Copyright 2026 Cloudbase Solutions Srl
# All Rights Reserved.

"""
Base test harness for Coriolis integration tests.

Starts conductor, scheduler, and worker services in-process using
oslo.messaging's and temporary database and messaging containers. Serves
the Coriolis REST API via cheroot on a random local port.
No Keystone or Barbican are required.

Must be run as root (scsi_debug block device setup requires it).
"""

import os
import shutil
import socket
import tempfile
from unittest import mock

from cheroot import wsgi as cheroot_wsgi
from oslo_config import cfg
from oslo_log import log as logging
from oslo_middleware import request_id as request_id_middleware
from oslo_service import wsgi as base_wsgi
import webob.dec

from coriolis import api as api_module
from coriolis.api.middleware import fault as fault_middleware
from coriolis.api.v1 import router as api_v1_router
from coriolis.api import wsgi as api_wsgi
from coriolis.conductor.rpc import server as conductor_rpc_server
from coriolis import conf as coriolis_conf
from coriolis import constants
from coriolis import context
from coriolis.db import api as db_api
from coriolis.db.sqlalchemy import api as sqlalchemy_api
from coriolis.db.sqlalchemy import migration as db_migration
from coriolis import policy as policy_module
from coriolis import rpc as rpc_module
from coriolis.scheduler.rpc import server as scheduler_rpc_server
from coriolis import service
from coriolis.tests.integration import utils as test_utils
from coriolis import utils as coriolis_utils
from coriolis.worker.rpc import server as worker_rpc_server

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

# Fixed project used for all test requests.
_TEST_PROJECT_ID = 'integration-project'


class _TestAPIRouter(api_v1_router.APIRouter):
    """V1 API router using APIMapper (no /{project_id}/ path prefix).

    The production router uses ProjectMapper which adds /{project_id}/ to
    every route. For tests the coriolisclient sends paths without a
    project_id segment, so we use the plain APIMapper instead.
    """

    def __init__(self):
        ext_mgr = self.ExtensionManager()
        mapper = api_module.APIMapper()
        self.resources = {}
        self._setup_routes(mapper, ext_mgr)
        self._setup_ext_routes(mapper, ext_mgr)
        self._setup_extensions(ext_mgr)
        base_wsgi.Router.__init__(self, mapper)


class _IntegrationHarness:
    """Integration tests infrastructure."""

    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.workdir = tempfile.mkdtemp(prefix="coriolis-integration-")
        self.lock_path = os.path.join(self.workdir, "locks")
        os.makedirs(self.lock_path)

        # Hard-coded in tox.ini.
        self._mysql_username = "root"
        self._mysql_password = "coriolis"
        self._mysql_database = "coriolis"
        self._rabbitmq_username = "coriolis"
        self._rabbitmq_password = "coriolis"

        coriolis_conf.init_common_opts()
        cfg.CONF([], project='coriolis', version='1.0.0',
                 default_config_files=[], default_config_dirs=[])
        transport_url = ('rabbit://%(user)s:%(password)s@localhost:5672/') % {
            "user": self._rabbitmq_username,
            "password": self._rabbitmq_password,
        }
        cfg.CONF.set_override('messaging_transport_url', transport_url)
        cfg.CONF.set_override(
            'providers', [_TEST_EXPORT_PROVIDER, _TEST_IMPORT_PROVIDER])
        db_url = ('mysql+pymysql://%(user)s:%(password)s'
                  '@localhost:3306/%(database)s') % {
            "user": self._mysql_username,
            "password": self._mysql_password,
            "database": self._mysql_database,
        }
        cfg.CONF.set_override(
            'connection', db_url, group='database')
        cfg.CONF.set_override(
            'retry_interval', 1, group='database')
        cfg.CONF.set_override(
            'lock_path', self.lock_path, group='oslo_concurrency')
        coriolis_utils.setup_logging()
        test_utils.init_scsi_debug()

        # Policy enforcer: reset so it re-reads the new CONF (no policy file).
        policy_module.reset()

        self._wsgi_server = None
        self._wsgi_server_thread = None
        self.api_port = None
        self._conductor_svc = None
        self._scheduler_svc = None
        self._worker_svc = None
        self._worker_host_svc = None

        # SQLAlchemy facade and RPC transport are module-level singletons;
        # reset them so they are re-created from the new CONF values.
        sqlalchemy_api._facade = None
        rpc_module._TRANSPORT = None

        engine = db_api.get_engine()
        db_migration.db_sync(engine)

        self._start_coriolis_services()

    def _start_coriolis_services(self):
        """Start conductor, scheduler, worker, and API in-process."""

        rpc_module.init()

        # Conductor: must start first so the worker can register with it.
        conductor_endpoint = conductor_rpc_server.ConductorServerEndpoint()
        conductor_endpoint._licensing_client = None
        conductor_endpoint._minion_manager_client_instance = mock.MagicMock()
        self._conductor_svc = service.MessagingService(
            constants.CONDUCTOR_MAIN_MESSAGING_TOPIC,
            [conductor_endpoint],
            conductor_rpc_server.VERSION,
            worker_count=1,
            init_rpc=False,
        )
        self._conductor_svc.start()

        self._scheduler_svc = service.MessagingService(
            constants.SCHEDULER_MAIN_MESSAGING_TOPIC,
            [scheduler_rpc_server.SchedulerServerEndpoint()],
            scheduler_rpc_server.VERSION,
            worker_count=1,
            init_rpc=False,
        )
        self._scheduler_svc.start()

        # Worker: constructor calls _register_worker_service() which makes a
        # blocking RPC call to the conductor, so the conductor must already be
        # listening.
        #
        # We reuse the same endpoint instance for both the main topic and the
        # host-specific topic (coriolis_worker.{hostname}) to avoid a double
        # service registration.
        _worker_endpoint = worker_rpc_server.WorkerServerEndpoint()
        self._worker_svc = service.MessagingService(
            constants.WORKER_MAIN_MESSAGING_TOPIC,
            [_worker_endpoint],
            worker_rpc_server.VERSION,
            worker_count=1,
            init_rpc=False,
        )
        self._worker_svc.start()

        _worker_host_topic = constants.SERVICE_MESSAGING_TOPIC_FORMAT % {
            "main_topic": constants.WORKER_MAIN_MESSAGING_TOPIC,
            "host": coriolis_utils.get_hostname(),
        }
        self._worker_host_svc = service.MessagingService(
            _worker_host_topic,
            [_worker_endpoint],
            worker_rpc_server.VERSION,
            worker_count=1,
            init_rpc=False,
        )
        self._worker_host_svc.start()

        # API: build the WSGI stack without keystonemiddleware and serve it
        # on a random local port.
        wsgi_app = _TestAPIRouter()
        wsgi_app = _NoAuthMiddleware(wsgi_app)
        wsgi_app = fault_middleware.FaultWrapper(wsgi_app)
        wsgi_app = request_id_middleware.RequestId(wsgi_app)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            s.listen(1)
            # Pick an available port.
            self.api_port = s.getsockname()[1]

        self._wsgi_server = cheroot_wsgi.Server(
            bind_addr=("127.0.0.1", self.api_port),
            wsgi_app=wsgi_app,
            server_name="coriolis-api",
        )
        self._wsgi_server.prepare()
        self._wsgi_server_thread = coriolis_utils.start_thread(
            self._wsgi_server.serve,
            daemon=True,
        )

    def teardown(self):
        LOG.info("Teardown initiated.")

        for svc in [self._worker_host_svc, self._worker_svc,
                    self._scheduler_svc, self._conductor_svc]:
            if not svc:
                continue
            try:
                svc.stop()
            except Exception:
                LOG.exception("Unable to stop service.")
                pass

        if self._wsgi_server:
            try:
                self._wsgi_server.stop()
                self._wsgi_server_thread.join()
            except Exception:
                LOG.exception("Unable to stop WSGI.")
                pass

        shutil.rmtree(self.workdir, True)
        try:
            test_utils.destroy_scsi_debug()
        except Exception:
            LOG.exception("Unable to cleanup scsi-debug device.")
            pass
