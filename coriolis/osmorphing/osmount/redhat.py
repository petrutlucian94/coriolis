# Copyright 2016 Cloudbase Solutions Srl
# All Rights Reserved.

from oslo_log import log as logging

from coriolis import utils
from coriolis.osmorphing.osmount import base

LOG = logging.getLogger(__name__)


class RedHatOSMountTools(base.BaseLinuxOSMountTools):
    def check_os(self):
        # make sure the package redhat-lsb-core is installed
        os_info = utils.get_linux_os_info(self._ssh)
        if os_info and os_info[0] in [
            'RedHatEnterpriseServer',
            'CentOS',
            'OracleServer',
            'rhel',
            'centos',
            'ol',
            'rocky',
        ]:
            return True

    def setup(self):
        super(RedHatOSMountTools, self).setup()
        self._exec_sudo_env_cmd("yum install -y psmisc cryptsetup")

        # We'll handle lvm2 separately, the post-installation hooks may fail
        # in case of minion pools that have already been used to replicate disks.
        #
        # It's caused by the fact that we're disabling lvm-metad and the LVM
        # udev rules before disk replication in order to prevent these logical
        # volumes from being mounted automatically.
        try:
            self._exec_cmd("which vgs")
            LOG.info("lvm2 already installed, skipping installation.")
        except Exception:
            # LVM2 missing, let's install it.
            self._exec_sudo_env_cmd("yum install -y lvm2")

        self._exec_cmd("sudo modprobe dm-mod")
        self._exec_cmd("sudo modprobe dm-crypt")
        self._exec_cmd("sudo rm -f /etc/lvm/devices/system.devices")
