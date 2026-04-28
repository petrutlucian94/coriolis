#!/bin/bash

set -ex

TEST_CONF_DIR=${TEST_CONF_DIR:-/tmp/coriolis/config}
TEST_LOG_DIR=${TEST_LOG_DIR:-/tmp/coriolis/log}
TEST_LOCK_DIR=$(mktemp -d)
mkdir -p $TEST_LOG_DIR
mkdir -p $TEST_CONF_DIR

# Start mysql container
sudo docker run -d --name coriolis-test-mysql \
    -e MYSQL_ROOT_PASSWORD=coriolis \
    -e MYSQL_DATABASE=coriolis \
    -p 3306:3306 \
    mariadb:10-jammy

# Start rabbitmq container
sudo docker run -d --name coriolis-test-rabbitmq \
	-e RABBITMQ_DEFAULT_USER=coriolis \
	-e RABBITMQ_DEFAULT_PASS=coriolis \
	-p 15672:15672 \
	-p 5672:5672 \
	rabbitmq:3.13

cat > $TEST_CONF_DIR/coriolis.conf <<EOF
[DEFAULT]
messaging_transport_url = rabbit://coriolis:coriolis@localhost:5672/

providers=coriolis.tests.integration.providers.test_provider.exp.TestExportProvider,coriolis.tests.integration.providers.test_provider.imp.TestImportProvider

debug = True
log_dir = $TEST_LOG_DIR

api_migration_workers = 1
messaging_workers = 1
worker_count = 1

[database]
connection = mysql+pymysql://coriolis:coriolis@localhost:3306/coriolis
retry_interval = 1

[oslo_concurrency]
local_path = $TEST_LOCK_DIR
EOF

cat > $TEST_CONF_DIR/api-paste.ini << EOF
[composite:coriolis-api]
use = call:coriolis.api:root_app_factory
/v1: coriolis-api-v1

[pipeline:coriolis-api-v1]
pipeline = request_id faultwrap noauthmiddleware apiv1

[app:apiv1]
paste.app_factory = coriolis.api.v1.router:APIRouter.factory

# Auth middleware that validates token against keystone
[filter:authtoken]
paste.filter_factory = keystonemiddleware.auth_token:filter_factory

[filter:faultwrap]
paste.filter_factory = coriolis.api.middleware.fault:FaultWrapper.factory

[filter:noauthmiddleware]
paste.filter_factory = coriolis.api.middleware.auth:NoAuthMiddleware.factory

[filter:request_id]
paste.filter_factory = oslo_middleware.request_id:RequestId.factory
EOF

coriolis-api --config-file $TEST_CONF_DIR/coriolis.conf &
coriolis-conductor --config-file $TEST_CONF_DIR/coriolis.conf &
coriolis-scheduler --config-file $TEST_CONF_DIR/coriolis.conf &
coriolis-worker --config-file $TEST_CONF_DIR/coriolis.conf &
