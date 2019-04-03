import oss2
from oss2.headers import RequestHeader
from .BaseClient import BaseClient
from aliyunsdkcore.request import CommonRequest
from aliyunsdkcore.client import AcsClient
from ..models.Snapshot import Snapshot
from ..models.Volume import Volume
from ..models.Attachment import Attachment
import json

class AliClient(BaseClient):
    def __init__(self, operation_name, configuration, directory_persistent, directory_work_list, poll_delay_time,
                 poll_maximum_time):
        super(AliClient, self).__init__(operation_name, configuration, directory_persistent, directory_work_list,
                                        poll_delay_time, poll_maximum_time)
        if configuration['credhub_url'] is None:
            self.__setCredentials(
                configuration['access_key_id'], configuration['secret_access_key'], configuration['region_name'])
        else:
            credentials = self._get_credentials_from_credhub(configuration)
            self.__setCredentials(
                credentials['access_key_id'], credentials['secret_access_key'], credentials['region_name'])
        self.endpoint = configuration['endpoint']
        # +-> Create compute and storage clients
        self.compute_client = self.create_compute_client()
        self.storage_client = self.create_storage_client()

        # +-> Check whether the given container exists
        self.container = self.get_container()
        if not self.container:
            msg = 'Could not find or access the given container.'
            self.last_operation(msg, 'failed')
            raise Exception(msg)
        
        # skipping some actions for blob operation
        if operation_name != 'blob_operation':
            # +-> Get the availability zone of the instance
            self.availability_zone = self._get_availability_zone_of_server(
                configuration['instance_id'])
            if not self.availability_zone:
                msg = 'Could not retrieve the availability zone of the instance.'
                self.last_operation(msg, 'failed')
                raise Exception(msg)

    def __setCredentials(self, access_key_id, secret_access_key, region_name):
        self.__aliCredentials = {
            'access_key_id': access_key_id,
            'secret_access_key': secret_access_key,
            'region_name': region_name
        }

    def create_compute_client(self):
        try:
            credentials = self.__aliCredentials
            compute_client = AcsClient(credentials['access_key_id'], credentials['secret_access_key'], credentials['region_name'], auto_retry=True,
            max_retry_time=10, timeout=30)
            return compute_client
        except Exception as error:
            raise Exception(
                'Creation of compute client failed: {}'.format(error))

    def create_storage_client(self):
        try:
            credentials = self.__aliCredentials
            storage_client = oss2.Auth(
                credentials['access_key_id'], credentials['secret_access_key'])
            return storage_client
        except Exception as error:
            raise Exception(
                'Creation of storage client failed: {}'.format(error))

    def _get_common_request(self, action_name, params, tags=None):
        request = CommonRequest()
        request.set_domain('ecs.aliyuncs.com')
        request.set_version('2014-05-26')
        request.set_action_name(action_name)
        i = 1
        if tags != None:
            for key in tags:
                paramKey = 'Tag.' + str(i) + '.Key'
                request.add_query_param(paramKey, key)
                paramVal = 'Tag.' + str(i) + '.Value'
                request.add_query_param(paramVal, tags[key])
                i += 1
        for key in params:
            request.add_query_param(key, params[key])
        return request

    def _get_availability_zone_of_server(self, instance_id):
        try:
            instance_details_req_params = {
                'InstanceIds' : [instance_id]
            }
            instance_details_request = self._get_common_request('DescribeInstances', instance_details_req_params)
            instance_details = self.compute_client.do_action_with_exception(instance_details_request)
            instance_details_json = json.loads(instance_details.decode('utf-8'))
            if len(instance_details_json['Instances']['Instance']) > 1:
                message = 'More than 1 instance found for with id {}'.format(
                instance_id)
                raise Exception(message)
            return instance_details_json['Instances']['Instance']['ZoneId']
        except Exception as error:
            self.logger.error(
                '[ALI] ERROR: Unable to determine the availability zone of instance {}.\n{}'.format(instance_id, error))
            return None

    def get_container(self):
        try:
            container = oss2.Bucket(self.storage_client, self.endpoint, self.CONTAINER)
            # Test if the container is accessible
            key = '{}/{}'.format(self.BLOB_PREFIX,
                                 'AccessTestByServiceFabrikPythonLibrary')
            container.put_object(key, 'This is a sample text')
            container.delete_object(key)
            return container
        except Exception as error:
            self.logger.error('[OSS] ERROR: Unable to find or access container {}.\n{}'.format(
                self.CONTAINER, error))
            return None

    def _upload_to_blobstore(self, blob_to_upload_path, blob_target_name):
        log_prefix = '[OSS] [UPLOAD]'

        if self.container:
            self.logger.info(
                '{} Started to upload the tarball to the object storage.'.format(log_prefix))
            try:
                requestHeader = RequestHeader()
                requestHeader.set_server_side_encryption("AES256")
                self.container.put_object_from_file(
                    blob_target_name, blob_to_upload_path, headers=requestHeader)
                self.logger.info('{} SUCCESS: blob_to_upload={}, blob_target_name={}, container={}'
                                 .format(log_prefix, blob_to_upload_path, blob_target_name, self.CONTAINER))
                return True
            except Exception as error:
                message = '{} ERROR: blob_to_upload={}, blob_target_name={}, container={}\n{}'.format(
                    log_prefix, blob_to_upload_path, blob_target_name, self.CONTAINER, error)
                self.logger.error(message)
                raise Exception(message)

    def _download_from_blobstore(self, blob_to_download_name, blob_download_target_path):
        log_prefix = '[OSS] [DOWNLOAD]'

        if self.container:
            self.logger.info('{} Started to download the tarball to target{}.'
                             .format(log_prefix, blob_download_target_path))
            try:
                self.container.get_object(
                    blob_to_download_name, blob_download_target_path).read()
                self.logger.info('{} SUCCESS: blob_to_download={}, blob_target_name={}, container={}'.format(
                    log_prefix, blob_to_download_name, self.CONTAINER, blob_download_target_path))
                return True
            except Exception as error:
                message = '{} ERROR: blob_to_download={}, blob_target_name={}, container={}\n{}'.format(
                    log_prefix, blob_to_download_name, blob_download_target_path, self.CONTAINER, error)
                self.logger.error(message)
                raise Exception(message)

    def _create_snapshot(self, volume_id, description='Service-Fabrik: Automated backup'):
        log_prefix = '[SNAPSHOT] [CREATE]'
        snapshot = None
        snapshot_creation_operation = None
        region_id = self.__aliCredentials['region_name']
        snapshot_name = self.generate_name_by_prefix(self.SNAPSHOT_PREFIX)
        try:
            snapshot_req_params = {
                'DiskId': volume_id,
                'SnapshotName': snapshot_name,
                'Description': description
            }
            self.logger.info('{} START for volume id {} with tags {} and snapshot name {}'.format(
                log_prefix, volume_id, self.tags, snapshot_name))
            snpshot_creation_request = self._get_common_request('CreateSnapshot', snapshot_req_params, self.tags)
            snapshot_creation_operation = self.compute_client.do_action_with_exception(snpshot_creation_request)
            snapshot_details_json = json.loads(snapshot_creation_operation.decode('utf-8'))
            snapshot_id = snapshot_details_json['SnapshotId']
            self._wait('Waiting for snapshot {} to get ready...'.format(snapshot_name),
                       (lambda snapshot_id, region_id: self._is_snapshot_ready(snapshot_id, region_id)),
                       None,
                       snapshot_id, region_id)

            snapshot = self.get_snapshot(snapshot_name)
            if snapshot and snapshot.status == 'accomplished':
                self._add_snapshot(snapshot.id)
                self.output_json['snapshotId'] = snapshot.id
                self.logger.info('{} SUCCESS: snapshot-id={}, volume-id={}, status={} with tags {}'.format(
                    log_prefix, snapshot.id, volume_id, snapshot.status, self.tags))
            else:
                message = '{} ERROR: snapshot-id={} status={}'.format(
                    log_prefix, snapshot_name, snapshot.status if snapshot else None)
                raise Exception(message)
        except Exception as error:
            message = '{} ERROR: volume-id={} and tags={}\n{}'.format(
                log_prefix, volume_id, self.tags, error)
            self.logger.error(message)
            if snapshot or snapshot_creation_operation:
                self.delete_snapshot(snapshot_id)
                snapshot = None
            raise Exception(message)

        return snapshot

    
    def _is_snapshot_ready(self, snapshot_id, region_id):
        """Gets the snapshot state.
        https://www.alibabacloud.com/help/doc-detail/25641.htm?spm=a2c63.p38356.a3.5.c880458dkimfOs#SnapshotType
        Status can be "progressing" "accomplished" or "failed"
        """
        # Try until snapshot state is either "accomplished" or "failed"
        # Retries for failures also
        try:
            get_snapshot_req_params = {
                'PageSize' : 10,
                'RegionId' : region_id,
                'SnapshotIds' : [snapshot_id]
            }
            get_snapshot_request = self._get_common_request('DescribeSnapshots', get_snapshot_req_params)
            snapshot_details = self.compute_client.do_action_with_exception(get_snapshot_request)
            snapshot_details_json = json.loads(snapshot_details.decode('utf-8'))
            if len(snapshot_details_json['Snapshots']['Snapshot']) > 1:
                self.logger.error('[ALI] ERROR: More than 1 snapshot found for with snapshot id {}'.format(
                snapshot_id))
                return False
            snapshot_state = snapshot_details_json['Snapshots']['Snapshot'][0]['Status']
            if snapshot_state in ('accomplished','failed'):
                return True
            return False
        except Exception as error:
            self.logger.error(
                '[ALI] ERROR: Unable to get snapshot details for snapshot id {}.\n{}'.format(snapshot_id, error))
            return False

    def _copy_snapshot(self, snapshot_id):
        return self.get_snapshot(snapshot_id)

    def snapshot_exists(self, snapshot_id):
        try:
            region_id = self.__aliCredentials['region_name']
            get_snapshot_req_params = {
                'PageSize' : 10,
                'RegionId' : region_id,
                'SnapshotIds' : [snapshot_id]
            }
            get_snapshot_request = self._get_common_request('DescribeSnapshots', get_snapshot_req_params)
            snapshot_details = self.compute_client.do_action_with_exception(get_snapshot_request)
            snapshot_details_json = json.loads(snapshot_details.decode('utf-8'))
            if len(snapshot_details_json['Snapshots']['Snapshot']) != 0:
                return False
            return True
        except Exception as error:
            self.logger.error('[ALI] ERROR: Error in getting snapshot {}.\n{}'.format(
                snapshot_id, error))
            return None


            if len(volume_details_json['Disks']['Disk']) != 0:
                return False
            return True
        except Exception as error:
            message = '[ALI] ERROR: Unable to get snapshot {}.\n{}'.format(
                snapshot_id, error)
            self.logger.error(message)
            raise Exception(message)

    def _delete_snapshot(self, snapshot_id):
        log_prefix = '[SNAPSHOT] [DELETE]'
        try:
            snapshot_deletion_req_params = {
                'SnapshotId': snapshot_id
            }
            snpshot_deletion_request = self._get_common_request('DeleteSnapshot', snapshot_deletion_req_params)
            snapshot_deletion_operation = self.compute_client.do_action_with_exception(snpshot_deletion_request)

            self._wait('Waiting for snapshot {} to be deleted...'.format(snapshot_id),
                       lambda id: not self.get_snapshot(id),
                       None,
                       snapshot_id)
            snapshot_exists = self.snapshot_exists(snapshot_id)

            # Check if snapshot exists, if not then it is successfully deleted, else raise exception
            if not snapshot_exists:
                self._remove_snapshot(snapshot_id)
                self.logger.info(
                    '{} SUCCESS: snapshot-id={}'.format(
                        log_prefix, snapshot_id))
                return True
            else:
                message = '{} ERROR: snapshot-id={}, snapshot still exists'.format(
                    log_prefix, snapshot_id)
                self.logger.error(message)
                raise Exception(message)
        except Exception as error:
            message = '{} ERROR: snapshot-id={}\n{}'.format(
                log_prefix, snapshot_id, error)
            self.logger.error(message)
            raise Exception(message)

    def get_snapshot(self, snapshot_id):
        try:
            region_id = self.__aliCredentials['region_name']
            get_snapshot_req_params = {
                'PageSize' : 10,
                'RegionId' : region_id,
                'SnapshotIds' : [snapshot_id]
            }
            get_snapshot_request = self._get_common_request('DescribeSnapshots', get_snapshot_req_params)
            snapshot_details = self.compute_client.do_action_with_exception(get_snapshot_request)
            snapshot_details_json = json.loads(snapshot_details.decode('utf-8'))
            if len(snapshot_details_json['Snapshots']['Snapshot']) > 1:
                message = 'More than 1 snapshot found for with id {}'.format(
                snapshot_id)
                raise Exception(message)
            if len(snapshot_details_json['Snapshots']['Snapshot']) == 0:
                return None
            snapshot = snapshot_details_json['Snapshots']['Snapshot'][0]
            return Snapshot(snapshot['SnapshotId'], snapshot['SourceDiskSize'], snapshot['CreationTime'], snapshot['Status'])
        except Exception as error:
            self.logger.error('[ALI] ERROR: Error in getting snapshot {}.\n{}'.format(
                snapshot_name, error))
            return None


    def get_attached_volumes_for_instance(self, instance_id):
        try:
            region_id = self.__aliCredentials['region_name']
            get_attached_volume_req_params = {
                'PageSize' : 10,
                'RegionId' : region_id,
                'InstanceId' : [instance_id]
            }
            get_attached_volume_request = self._get_common_request('DescribeDisks', get_attached_volume_req_params)
            volume_details = self.compute_client.do_action_with_exception(get_attached_volume_request)
            volume_details_json = json.loads(volume_details.decode('utf-8'))

            volume_list = []
            for disk in volume_details_json['Disks']['Disk']:
                device = disk['Device']
                #  Device information of the related instance, such as /dev/xvdb
                # It is null unless the Status  is In Use (In_use).
                if device is not None:
                    volume_list.append(
                        Volume(disk['DiskId'], disk['Status'], disk['Size'], device))
            return volume_list
        except Exception as error:
            self.logger.error(
                '[ALI] ERROR: Unable to find or access attached volume for instance_id {}.{}'.format(
                    instance_id, error))
            return []

    def get_persistent_volume_for_instance(self, instance_id):
        device = self.shell(
            'cat {} | grep {}'.format(self.FILE_MOUNTS, self.DIRECTORY_PERSISTENT)).split(' ')[0][:8]
        # --> /dev/vdb on machine will be /dev/xvdb on AliCloud for "I/O Optimized" instances
        # --> https://www.alibabacloud.com/help/doc-detail/25426.htm
        device = device.replace('/v', '/xv')
        for volume in self.get_attached_volumes_for_instance(instance_id):
            if volume.device == device:
                self._add_volume_device(volume.id, device)
                return volume
        return None

    def _is_volume_ready(self, disk_id, region_id, attached_vol=False):
        """Gets the disk state.
        https://www.alibabacloud.com/help/doc-detail/25626.htm?spm=a2c63.p38356.879954.9.67f065dd0XjNkR#DiskItemType
        Status can be "In_use" or "Available" or "Attaching" or "Detaching" or "Creating" or "ReIniting"
        """
        # Try until disk state is either "Available" or "In_use"
        # Retries for failures also
        try:
            get_disk_req_params = {
                'PageSize' : 10,
                'RegionId' : region_id,
                'DiskIds' : [disk_id]
            }
            get_disk_request = self._get_common_request('DescribeDisks', get_disk_req_params)
            disk_details = self.compute_client.do_action_with_exception(get_disk_request)
            disk_details_json = json.loads(disk_details.decode('utf-8'))
            if len(disk_details_json['Disks']['Disk']) > 1:
                self.logger.error('[ALI] ERROR: More than 1 disks found for with disk_id {}'.format(
                disk_id))
                return False
            disk_state = disk_details_json['Disks']['Disk'][0]['Status']
            if disk_state in ('Available', 'In_use'):
                # State has to be In_use once attached
                if attached_vol and disk_state == 'Available':
                    return False
                return True
            return False
        except Exception as error:
            self.logger.error(
                '[ALI] ERROR: Unable to get snapshot details for snapshot id {}.\n{}'.format(snapshot_id, error))
            return False

    def get_volume(self, volume_id):
        try:
            region_id = self.__aliCredentials['region_name']
            get_volume_req_params = {
                'PageSize' : 10,
                'RegionId' : region_id,
                'DiskIds' : [volume_id]
            }
            get_volume_request = self._get_common_request('DescribeDisks', get_volume_req_params)
            volume_details = self.compute_client.do_action_with_exception(get_volume_request)
            volume_details_json = json.loads(volume_details.decode('utf-8'))
            if len(volume_details_json['Disks']['Disk']) > 1:
                message = 'More than 1 volumes found for with id {}'.format(
                volume_id)
                raise Exception(message)
            if len(volume_details_json['Disks']['Disk']) == 0:
                return None
            volume = volume_details_json['Disks']['Disk'][0]
            return Volume(volume['DiskId'], volume['Status'], volume['Size'])
        except Exception as error:
            self.logger.error(
                '[ALI] ERROR: Unable to find or access volume/disk {}.\n{}'.format(
                    volume_id, error))
            return None

    def _create_volume(self, size, snapshot_id=None, volume_type = 'cloud_ssd'):
        log_prefix = '[VOLUME] [CREATE]'
        volume = None
        disk_creation_operation = None
        disk_name = self.generate_name_by_prefix(self.DISK_PREFIX)
        region_id = self.__aliCredentials['region_name']
        disk_creation_req_params = {
            'RegionId' : region_id,
            'ZoneId' : self.availability_zone,
            'DiskName' : disk_name,
            'DiskCategory' : volume_type,
            'Encrypted': True,
            'Size' : size
        }
        try:
            if snapshot_id is not None:
                disk_creation_req_params['SnapshotId'] = snapshot_id
            
            disk_creation_request = self._get_common_request('CreateDisk', disk_creation_req_params, self.tags)
            disk_creation_operation = self.compute_client.do_action_with_exception(disk_creation_request)
            volume_details_json = json.loads(disk_creation_operation.decode('utf-8'))
            disk_id = volume_details_json['DiskId']

            self._wait('Waiting for volume {} to get ready...'.format(disk_name),
                       (lambda disk_id, region_id: self._is_volume_ready(disk_id, region_id)),
                       None,
                       disk_id, region_id)

            volume = self.get_volume(disk_id)
            if volume and volume.status in ('Available', 'In_use'):
                self._add_volume(volume.id)
                self.logger.info('{} SUCCESS: volume-id={} with tags={} '.format(
                    log_prefix, volume.id, self.tags))
            else:
                message = '{} ERROR: volume-id={} status={}'.format(
                    log_prefix, volume.id if volume else None, volume.status if volume else None)
                raise Exception(message)
        except Exception as error:
            message = '{} ERROR: size={}\n{}'.format(log_prefix, size, error)
            self.logger.error(message)
            if volume:
                self.delete_volume(volume.id)
                volume = None
            raise Exception(message)
        return volume

    def volume_exists(self, volume_id):
        try:
            region_id = self.__aliCredentials['region_name']
            get_volume_req_params = {
                'PageSize' : 10,
                'RegionId' : region_id,
                'DiskIds' : [volume_id]
            }
            get_volume_request = self._get_common_request('DescribeDisks', get_volume_req_params)
            volume_details = self.compute_client.do_action_with_exception(get_volume_request)
            volume_details_json = json.loads(volume_details.decode('utf-8'))
            if len(volume_details_json['Disks']['Disk']) != 0:
                return False
            return True
        except Exception as error:
            message = '[ALI] ERROR: Unable to get disk {}.\n{}'.format(
                volume_id, error)
            self.logger.error(message)
            raise Exception(message)

    def _delete_volume(self, volume_id):
        log_prefix = '[VOLUME] [DELETE]'
        try:
            volume_deletion_req_params = {
                'DiskId': volume_id
            }
            volume_deletion_request = self._get_common_request('DeleteDisk', volume_deletion_req_params)
            volume_deletion_operation = self.compute_client.do_action_with_exception(snpshot_deletion_request)

            self._wait('Waiting for disk {} to be deleted...'.format(volume_id),
                       lambda id: not self.get_volume(id),
                       None,
                       volume_id)
            
            volume_exists = self.volume_exists(volume_id)

            # Check if volume exists, if not then it is successfully deleted, else raise exception
            if not volume_exists:
                self._remove_volume(volume_id)
                self.logger.info(
                    '{} SUCCESS: volume-id={}'.format(
                        log_prefix, volume_id))
                return True
            else:
                message = '{} ERROR: volume-id={}, volume still exists'.format(
                    log_prefix, volume_id)
                self.logger.error(message)
                raise Exception(message)
        except Exception as error:
            message = '{} ERROR: volume-id={}\n{}'.format(
                log_prefix, volume_id, error)
            self.logger.error(message)
            raise Exception(message)

    def _find_volume_device(self, volume_id):
        try:
            device = None
            region_id = self.__aliCredentials['region_name']
            get_volume_req_params = {
                'PageSize' : 10,
                'RegionId' : region_id,
                'DiskIds' : [volume_id]
            }
            get_volume_request = self._get_common_request('DescribeDisks', get_volume_req_params)
            volume_details = self.compute_client.do_action_with_exception(get_volume_request)
            volume_details_json = json.loads(volume_details.decode('utf-8'))
            return volume_details_json['Disks']['Disk'][0]['Device']
        except Exception as error:
            self.logger.error(
                '[ALI] ERROR: Unable to find device for attached volume {}.{}'.format(
                    volume_id, error))
            return None

    def _create_attachment(self, volume_id, instance_id):
        log_prefix = '[ATTACHMENT] [CREATE]'
        attachment = None
        device = None
        attachment_creation_operation = None
        try:
            region_id = self.__aliCredentials['region_name']
            attachment_req_params = {
                'InstanceId' : instance_id,
                'DiskId' : volume_id
            }
            attachment_request = self._get_common_request('AttachDisk', attachment_req_params)
            attachment_creation_operation = self.compute_client.do_action_with_exception(attachment_request)
            attachment_details_json = json.loads(attachment_creation_operation.decode('utf-8'))
            
            self._wait('Waiting for volume {} to get ready...'.format(volume_id),
                       (lambda disk_id, region_id: self._is_volume_ready(volume_id, region_id, True)),
                       None,
                       volume_id, region_id)
            
            # Raise exception if device returned in None,
            # as it might mean that disk was not attached properly
            device = self._find_volume_device(volume_id)
            if device is not None:
                self.logger.info(
                    'Attached volume-id={}, device={}'.format(volume_id, device))
                self._add_volume_device(volume_id, device)
            else:
                message = '{} ERROR: Device returned for volume-id={} is None'.format(
                    log_prefix, volume_id)
                raise Exception(message)

            attachment = Attachment(0, volume_id, instance_id)
            self._add_attachment(volume_id, instance_id)
            self.logger.info(
                '{} SUCCESS: volume-id={}, instance-id={}'.format(
                    log_prefix, volume_id, instance_id))
        except Exception as error:
            message = '{} ERROR: volume-id={}, instance-id={}\n{}'.format(
                log_prefix, volume_id, instance_id, error)
            self.logger.error(message)

            # The following lines are a workaround in case of inconsistency:
            # The attachment process may end with throwing an Exception,
            # but the attachment has been successful. Therefore, we must
            # check whether the volume is attached and if yes, trigger the detachment
            if attachment_creation_operation:
                if device:
                    self.logger.warning('[VOLUME] [DELETE] Volume is attached although the attaching process failed, '
                                    'triggering detachment')
                    attachment = True
                if attachment:
                    self.delete_attachment(volume_id, instance_id)
                    attachment = None
            raise Exception(message)

    def _delete_attachment(self, volume_id, instance_id):
        log_prefix = '[ATTACHMENT] [DELETE]'
        try:
            region_id = self.__aliCredentials['region_name']
            delete_attachment_req_params = {
                'InstanceId' : instance_id,
                'DiskId' : volume_id
            }
            delete_attachment_request = self._get_common_request('DetachDisk', delete_attachment_req_params)
            attachment_deletion_operation = self.compute_client.do_action_with_exception(delete_attachment_request)
            
            self._wait('Waiting for attachment of volume {} to be deleted...'.format(volume_id),
                       (lambda disk_id, region_id: self._is_volume_ready(volume_id, region_id)),
                       None,
                       volume_id, region_id)
            
            self._remove_volume_device(volume_id)
            self._remove_attachment(volume_id, instance_id)
            self.logger.info(
                '{} SUCCESS: volume-id={}, instance-id={}'.format(log_prefix, volume_id, instance_id))
            return True
        except Exception as error:
            message = '{} ERROR: volume-id={}, instance-id={}\n{}'.format(
                log_prefix, volume_id, instance_id, error)
            self.logger.error(message)
            raise Exception(message)

    def get_mountpoint(self, volume_id, partition=None):
        device = self._get_device_of_volume(volume_id)
        if not device:
            return None
        # --> /dev/vdb on machine will be /dev/xvdb on AliCloud for "I/O Optimized" instances
        # --> https://www.alibabacloud.com/help/doc-detail/25426.htm
        device = device.replace('/xv', '/v')
        if partition:
            device += partition
        return device