# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Helper code for the iSER volume driver.

"""
import os
import re

from oslo.config import cfg

from cinder import exception
from cinder import flags
from cinder.openstack.common import log as logging
from cinder import utils

LOG = logging.getLogger(__name__)

iser_helper_opt = [cfg.StrOpt('iser_helper',
                              default='tgtadm',
                              help='iser target user-land tool to use'),
                   cfg.StrOpt('volumes_dir',
                              default='$state_path/volumes',
                              help='Volume configuration file storage '
                                   'directory'
                              )
                   ]

FLAGS = flags.FLAGS
FLAGS.register_opts(iser_helper_opt)
FLAGS.import_opt('volume_name_template', 'cinder.db')


class TargetAdmin(object):
    """iSER target administration.

    Base class for iSER target admin helpers.
    """

    def __init__(self, cmd, execute):
        self._cmd = cmd
        self.set_execute(execute)

    def set_execute(self, execute):
        """Set the function to be used to execute commands."""
        self._execute = execute

    def _run(self, *args, **kwargs):
        self._execute(self._cmd, *args, run_as_root=True, **kwargs)

    def create_iser_target(self, name, tid, lun, path,
                           chap_auth=None, **kwargs):
        """Create a iSER target and logical unit"""
        raise NotImplementedError()

    def remove_iser_target(self, tid, lun, vol_id, **kwargs):
        """Remove a iSER target and logical unit"""
        raise NotImplementedError()

    def _new_target(self, name, tid, **kwargs):
        """Create a new iSER target."""
        raise NotImplementedError()

    def _delete_target(self, tid, **kwargs):
        """Delete a target."""
        raise NotImplementedError()

    def show_target(self, tid, iqn=None, **kwargs):
        """Query the given target ID."""
        raise NotImplementedError()

    def _new_logicalunit(self, tid, lun, path, **kwargs):
        """Create a new LUN on a target using the supplied path."""
        raise NotImplementedError()

    def _delete_logicalunit(self, tid, lun, **kwargs):
        """Delete a logical unit from a target."""
        raise NotImplementedError()


class TgtAdm(TargetAdmin):
    """iSER target administration using tgtadm."""

    def __init__(self, execute=utils.execute):
        super(TgtAdm, self).__init__('tgtadm', execute)

    def _get_target(self, iqn):
        (out, err) = self._execute('tgt-admin', '--show', run_as_root=True)
        lines = out.split('\n')
        for line in lines:
            if iqn in line:
                parsed = line.split()
                tid = parsed[1]
                return tid[:-1]

        return None

    def create_iser_target(self, name, tid, lun, path,
                           chap_auth=None, **kwargs):
        # Note(jdg) tid and lun aren't used by TgtAdm but remain for
        # compatibility

        utils.ensure_tree(FLAGS.volumes_dir)

        vol_id = name.split(':')[1]
        if chap_auth is None:
            volume_conf = """
                <target %s>
                    driver iser
                    backing-store %s
                </target>
            """ % (name, path)
        else:
            volume_conf = """
                <target %s>
                    driver iser
                    backing-store %s
                    %s
                </target>
            """ % (name, path, chap_auth)

        LOG.info(_('Creating iser_target for: %s') % vol_id)
        volumes_dir = FLAGS.volumes_dir
        volume_path = os.path.join(volumes_dir, vol_id)

        f = open(volume_path, 'w+')
        f.write(volume_conf)
        f.close()

        old_persist_file = None
        old_name = kwargs.get('old_name', None)
        if old_name is not None:
            old_persist_file = os.path.join(volumes_dir, old_name)

        try:
            (out, err) = self._execute('tgt-admin',
                                       '--update',
                                       name,
                                       run_as_root=True)
        except exception.ProcessExecutionError, e:
            LOG.error(_("Failed to create iser target for volume "
                        "id:%(vol_id)s.") % locals())

            #Don't forget to remove the persistent file we created
            os.unlink(volume_path)
            raise exception.ISERTargetCreateFailed(volume_id=vol_id)

        iqn = '%s%s' % (FLAGS.iser_target_prefix, vol_id)
        tid = self._get_target(iqn)
        if tid is None:
            LOG.error(_("Failed to create iser target for volume "
                        "id:%(vol_id)s. Please ensure your tgtd config file "
                        "contains 'include %(volumes_dir)s/*'") % locals())
            raise exception.NotFound()

        if old_persist_file is not None and os.path.exists(old_persist_file):
            os.unlink(old_persist_file)

        return tid

    def remove_iser_target(self, tid, lun, vol_id, **kwargs):
        LOG.info(_('Removing iser_target for: %s') % vol_id)
        vol_uuid_file = FLAGS.volume_name_template % vol_id
        volume_path = os.path.join(FLAGS.volumes_dir, vol_uuid_file)
        if os.path.isfile(volume_path):
            iqn = '%s%s' % (FLAGS.iser_target_prefix,
                            vol_uuid_file)
        else:
            raise exception.ISERTargetRemoveFailed(volume_id=vol_id)
        try:
            # NOTE(vish): --force is a workaround for bug:
            #             https://bugs.launchpad.net/cinder/+bug/1159948
            self._execute('tgt-admin',
                          '--force',
                          '--delete',
                          iqn,
                          run_as_root=True)
        except exception.ProcessExecutionError, e:
            LOG.error(_("Failed to remove iser target for volume "
                        "id:%(vol_id)s.") % locals())
            raise exception.ISERTargetRemoveFailed(volume_id=vol_id)

        os.unlink(volume_path)

    def show_target(self, tid, iqn=None, **kwargs):
        if iqn is None:
            raise exception.InvalidParameterValue(
                err=_('valid iqn needed for show_target'))

        tid = self._get_target(iqn)
        if tid is None:
            raise exception.NotFound()


class FakeIserHelper(object):

    def __init__(self):
        self.tid = 1

    def set_execute(self, execute):
        self._execute = execute

    def create_iser_target(self, *args, **kwargs):
        self.tid += 1
        return self.tid


def get_target_admin():
    if FLAGS.iser_helper == 'tgtadm':
        return TgtAdm()
    elif FLAGS.iser_helper == 'fake':
        return FakeIserHelper()
