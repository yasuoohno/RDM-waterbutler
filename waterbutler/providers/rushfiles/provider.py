import json
import datetime
from uuid import uuid4
from enum import IntEnum
from urllib import parse
from typing import List, Tuple, Union

from waterbutler.core import provider, streams
from waterbutler.core.path import WaterButlerPath
from waterbutler.core import exceptions

from waterbutler.providers.rushfiles import settings
from waterbutler.providers.rushfiles.metadata import (RushFilesPath, RushFilesRevision,
                                                        BaseRushFilesMetadata,
                                                        RushFilesFileMetadata,
                                                        RushFilesFolderMetadata)


class Attributes(IntEnum):
    DIRECTORY = 16
    ARCHIVE = 32
    NORMAL = 128


class ClientJournalEventType(IntEnum):
    CREATE = 0
    DELETE = 1
    UPDATE = 3
    MOVE = 16


class EmptyResponse:
    def __init__(self):
        self.status = 200
        self.headers = {'Content-Length': 0}
        self.content = streams.EmptyStream()

    async def release(self):
        return


class RushFilesProvider(provider.BaseProvider):
    """
    Provider for RushFiles cloud storage service.
    """
    NAME = 'rushfiles'
    CHUNK_SIZE = settings.CHUNK_SIZE
    DEVICE_ID = settings.DEVICE_ID

    def __init__(self, auth: dict, credentials: dict, settings: dict) -> None:
        super().__init__(auth, credentials, settings)
        self.token = self.credentials['token']
        self.share = self.settings['share']

    async def validate_v1_path(self, path: str, **kwargs) -> RushFilesPath:
        rf_path = await self.validate_path(path, **kwargs)

        if not rf_path.identifier:
            raise exceptions.NotFoundError(str(rf_path))

        return rf_path

    async def validate_path(self, path: str, **kwargs) -> RushFilesPath:
        if path == '/':
            return RushFilesPath('/', _ids=[self.share['id']], folder=True)

        is_folder = path.endswith('/')
        children_path_list = [parse.unquote(x) for x in path.strip('/').split('/')]
        inter_id_list = [self.share['id']]
        current_inter_id = self.share['id']

        for i, child in enumerate(children_path_list):
            response = await self.make_request(
                'GET',
                self._build_clientgateway_url(str(self.share['id']), 'virtualfiles', str(current_inter_id), 'children'),
                expects=(200, 404,),
                throws=exceptions.MetadataError,
            )
            if response.status == 404:
                raise exceptions.NotFoundError(path)
            res = await response.json()
            current_inter_id, index = self._search_inter_id(res, child)
            inter_id_list.append(current_inter_id)
            if not current_inter_id:
                if i == len(children_path_list) - 1:
                    return RushFilesPath(path, _ids=inter_id_list)
                raise exceptions.NotFoundError(path)

        if res['Data'][index]['IsFile'] == is_folder:
            raise exceptions.NotFoundError(path)

        return RushFilesPath(path, folder=is_folder, _ids=inter_id_list)

    async def revalidate_path(self,
                              base: WaterButlerPath,
                              name: str,
                              folder: bool = None) -> RushFilesPath:
        response = await self.make_request(
            'GET',
            self._build_clientgateway_url(str(self.share['id']), 'virtualfiles', base.identifier, 'children'),
            expects=(200, 404,),
            throws=exceptions.MetadataError,
        )
        if response.status == 404:
            raise exceptions.NotFoundError(name)
        res = await response.json()
        child_id, index = self._search_inter_id(res, name)

        if child_id is not None:
            if res['Data'][index]['IsFile'] == folder:
                raise exceptions.NotFoundError(name)

        return base.child(name, _id=child_id, folder=folder)

    def can_duplicate_names(self) -> bool:
        return False

    @property
    def default_headers(self) -> dict:
        return {'authorization': 'Bearer {}'.format(self.token)}

    def can_intra_move(self, other: provider.BaseProvider, path: WaterButlerPath = None) -> bool:
        return self == other

    def can_intra_copy(self, other: provider.BaseProvider, path=None) -> bool:
        # rushfiles can copy a folder, but do not copy the files inside it.
        return self == other and (path and path.is_file)

    async def intra_move(self,  # type: ignore
                         dest_provider: provider.BaseProvider,
                         src_path: WaterButlerPath,
                         dest_path: WaterButlerPath) -> Tuple[BaseRushFilesMetadata, bool]:
        if dest_path.identifier:
            await dest_provider.delete(dest_path)

        src_metadata = await self._file_metadata(src_path, raw=True)
        request_body = json.dumps({
            'RfVirtualFile': {
                'InternalName': src_path.identifier,
                'ShareId': self.share['id'],
                'ParrentId': dest_path.parent.identifier,
                'EndOfFile': src_metadata['EndOfFile'] if src_path.is_file else 0,
                'Tick': 0,
                'PublicName': dest_path.name,
                'CreationTime': src_metadata['CreationTime'],
                'LastAccessTime': src_metadata['LastAccessTime'],
                'LastWriteTime': src_metadata['LastWriteTime'],
                'Attributes': src_metadata['Attributes'],
            },
            'TransmitId': self._generate_uuid(),
            'ClientJournalEventType': ClientJournalEventType.MOVE,
            'DeviceId': self.DEVICE_ID,
        })

        async with self.request(
            'PUT',
            self._build_filecache_url(str(self.share['id']), 'files', src_path.identifier),
            data=request_body,
            headers={'Content-Type': 'application/json'},
            expects=(200, ),
            throws=exceptions.IntraMoveError,
        ) as response:
            resp = await response.json()
            data = resp['Data']['ClientJournalEvent']['RfVirtualFile']

        created = dest_path.identifier is None
        dest_path.parts[-1]._id = data['InternalName']
        dest_path.rename(data['PublicName'])

        if dest_path.is_dir:
            metadata = RushFilesFolderMetadata(data, dest_path)
            metadata.children = await self._folder_metadata(dest_path)
            return metadata, created

        return RushFilesFileMetadata(data, dest_path), created

    async def intra_copy(self,
                         dest_provider: provider.BaseProvider,
                         src_path: WaterButlerPath,
                         dest_path: WaterButlerPath) -> Tuple[RushFilesFileMetadata, bool]:
        if dest_path.identifier:
            await dest_provider.delete(dest_path)
            dest_path = dest_path.parent.child(dest_path.name)

        # only file
        async with self.request(
            'POST',
            self._build_filecache_url(str(self.share['id']), 'files', src_path.identifier, 'clone'),
            data=json.dumps({
                'DestinationParentId': dest_path.parent.identifier,
                'DeviceId': self.DEVICE_ID,
                'DestinationShareId': dest_provider.share['id'],
            }),
            headers={'Content-Type': 'application/json'},
            expects=(201, ),
            throws=exceptions.IntraCopyError,
        ) as response:
            resp = await response.json()
            data = resp['Data']['ClientJournalEvent']['RfVirtualFile']

        clone_result_path = dest_path.parent.child(data['PublicName'], _id=data['InternalName'])
        if clone_result_path == dest_path:
            # Cloned file is exactly the same as destination path. Can return right away.
            return RushFilesFileMetadata(data, clone_result_path), True
        else:
            # Destination does not match (cloned file should be renamed or destination existed and we have a duplicate).
            return await self.intra_move(dest_provider, clone_result_path, dest_path)

    async def download(self,  # type: ignore
                       path: WaterButlerPath,
                       revision: str = None,
                       range: Tuple[int, int] = None,
                       **kwargs) -> streams.ResponseStreamReader:
        if path.identifier is None:
            raise exceptions.DownloadError('"{}" not found'.format(str(path)), code=404)

        if path.is_dir:
            raise exceptions.DownloadError('Path must be a file', code=404)

        metadata = await self.metadata(path, revision=revision)

        if metadata.size == 0:
            return streams.ResponseStreamReader(EmptyResponse())

        resp = await self.make_request(
            'GET',
            self._build_filecache_url(str(self.share['id']), 'files', metadata.upload_name),
            range=range,
            expects=(200, 206,),
            throws=exceptions.DownloadError,
        )
        return streams.ResponseStreamReader(resp)

    async def upload(self,
                     stream,
                     path: WaterButlerPath,
                     *args,
                     **kwargs) -> Tuple[RushFilesFileMetadata, bool]:
        created = not await self.exists(path)

        if stream.size > 0:
            data = await self._upload_request(stream, path, created)
            data = await self._upload_file(stream, data['Data']['Url'])
        else:
            data = await self._upload_request(stream, path, created)

        return RushFilesFileMetadata(data['Data']['ClientJournalEvent']['RfVirtualFile'], path), created

    async def _upload_request(self, stream, path, created):
        now = self._get_time_for_sending()
        if not created:
            metadata = await self.metadata(path)
        request_body = json.dumps({
            'RfVirtualFile': {
                'InternalName': path.identifier if not created else '',
                'ShareId': self.share['id'],
                'ParrentId': path.parent.identifier,
                'EndOfFile': str(stream.size),
                'Tick': 0,  # Tick is required, but ignored so can be set to any value
                'PublicName': path.name,
                'CreationTime': now if created else metadata.created_utc,
                'LastAccessTime': now,
                'LastWriteTime': now,
                'Attributes': Attributes.NORMAL,
            },
            'TransmitId': self._generate_uuid(),
            'ClientJournalEventType': ClientJournalEventType.CREATE if created else ClientJournalEventType.UPDATE,
            'DeviceId': self.DEVICE_ID,
        })

        if created:
            upload_url = self._build_filecache_url(str(self.share['id']), 'files')
        else:
            upload_url = self._build_filecache_url(str(self.share['id']), 'files', path.identifier)

        response = await self.make_request(
            'POST' if created else 'PUT',
            upload_url,
            data=request_body,
            headers={'Content-Type': 'application/json'},
            expects=(200, 202, ),
            throws=exceptions.UploadError,
        )
        data = await response.json()

        return data

    async def _upload_file(self, stream, uploadUrl):
        """
        RushFiles has a limit on request's size. File might need to be divided into chunks and uploaded in multiple requests.
        """
        position = 0
        while position < stream.size:
            end = min(position + self.CHUNK_SIZE, stream.size)
            response = await self.make_request(
                'PUT',
                uploadUrl,
                headers={
                    'Content-Type': 'application/octet-stream',
                    'Content-Range': 'bytes ' + str(position) + '-' + str(end - 1) + '/*',
                    'Content-Length': str(end - position)
                },
                data=await stream.read(end - position),
                expects=(200, 201, 202),
                throws=exceptions.UploadError,
            )
            position = end
            print(response.status)

        return await response.json()

    async def delete(self,  # type: ignore
                     path: WaterButlerPath,
                     **kwargs) -> None:
        if not path.identifier:
            raise exceptions.NotFoundError(str(path))
        if path.is_root:
            raise exceptions.DeleteError(
                'root cannot be deleted',
                code=400
            )

        if path.is_folder:
            return await self._delete_folder(path)
        else:
            return await self._delete_virtual_file(path)

    async def _delete_folder(self,
                             path: WaterButlerPath) -> None:
        for item in await self._folder_metadata(path):
            if item.is_file:
                await self._delete_virtual_file(item.path_obj)
            else:
                await self._delete_folder(item.path_obj)

        await self._delete_virtual_file(path)

        return

    async def _delete_virtual_file(self,
                           path: WaterButlerPath) -> None:
        response = await self.make_request(
            'DELETE',
            self._build_filecache_url(str(self.share['id']), 'files', path.identifier),
            data=json.dumps({
                'TransmitId': self._generate_uuid(),
                'ClientJournalEventType': ClientJournalEventType.DELETE,
                'DeviceId': self.DEVICE_ID,
            }),
            headers={'Content-Type': 'application/json'},
            expects=(200, 400, 404,),
            throws=exceptions.DeleteError,
        )

        if response.status == 400 or response.status == 404:
            raise exceptions.NotFoundError(str(path))

        return

    async def metadata(self,  # type: ignore
                       path: WaterButlerPath,
                       raw: bool = False,
                       revision=None,
                       **kwargs) -> Union[dict, BaseRushFilesMetadata,
                                          List[Union[BaseRushFilesMetadata, dict]]]:
        if path.identifier is None:
            raise exceptions.MetadataError('{} not found'.format(str(path)), code=404)

        if path.is_dir:
            return await self._folder_metadata(path, raw=raw)

        return await self._file_metadata(path, revision=revision, raw=raw)

    async def revisions(self, path: WaterButlerPath,  # type: ignore
                        **kwargs) -> List[RushFilesRevision]:

        if path.identifier is None:
            raise exceptions.NotFoundError(str(path))

        async with self.request(
            'GET',
            self._build_clientgateway_url(str(self.share['id']), 'virtualfiles', path.identifier, 'history'),
            expects=(200, ),
            throws=exceptions.RevisionsError
        ) as response:
            data = await response.json()
            revisions = data['Data']

        return [RushFilesRevision(each['File']) for each in revisions]

    async def create_folder(self,
                            path: WaterButlerPath,
                            folder_precheck: bool = True,
                            **kwargs) -> RushFilesFolderMetadata:
        WaterButlerPath.validate_folder(path)

        if folder_precheck:
            if path.identifier:
                raise exceptions.FolderNamingConflict(path.name)

        now = self._get_time_for_sending()
        request_body = json.dumps({
            'RfVirtualFile': {
                'ShareId': self.share['id'],
                'ParrentId': path.parent.identifier,
                'EndOfFile': 0,
                'PublicName': path.name,
                'CreationTime': now,
                'LastAccessTime': now,
                'LastWriteTime': now,
                'Attributes': Attributes.DIRECTORY,
            },
            'TransmitId': self._generate_uuid(),
            'ClientJournalEventType': ClientJournalEventType.CREATE,
            'DeviceId': self.DEVICE_ID,
        })

        async with self.request(
            'POST',
            self._build_filecache_url(str(self.share['id']), 'files'),
            data=request_body,
            headers={'Content-Type': 'application/json'},
            expects=(200, ),
            throws=exceptions.CreateFolderError,
        ) as response:
            resp = await response.json()
            return RushFilesFolderMetadata(resp['Data']['ClientJournalEvent']['RfVirtualFile'], path)

    def path_from_metadata(self, parent_path, metadata) -> RushFilesPath:
        return parent_path.child(metadata.name, _id=metadata.internal_name,
                                 folder=metadata.is_folder)

    def _build_filecache_url(self, *segments, **query):
        return provider.build_url('https://filecache01.{}'.format(self.share['domain']), 'api', 'shares', *segments, **query)

    def _build_clientgateway_url(self, *segments, **query):
        return provider.build_url('https://clientgateway.{}'.format(self.share['domain']), 'api', 'shares', *segments, **query)

    async def _folder_metadata(self,
                               path: WaterButlerPath,
                               raw: bool = False) -> List[Union[BaseRushFilesMetadata, dict]]:
        share_id = self.share['id']
        inter_id = path.identifier

        response = await self.make_request(
            'GET',
            self._build_clientgateway_url(str(share_id), 'virtualfiles', inter_id, 'children'),
            expects=(200, 404,),
            throws=exceptions.MetadataError,
        )

        if response.status == 404:
            raise exceptions.NotFoundError(path)
        res = await response.json()

        if raw:
            return res['Data']
        else:
            ret = []
            for data in res['Data']:
                if data['IsFile']:
                    ret.append(RushFilesFileMetadata(data, path.child(data['PublicName'], _id=data['InternalName'], folder=False)))
                else:
                    ret.append(RushFilesFolderMetadata(data, path.child(data['PublicName'], _id=data['InternalName'], folder=True)))
            return ret

    async def _file_metadata(self,
                             path: WaterButlerPath,
                             revision: str = None,
                             raw: bool = False) -> Union[dict, RushFilesFileMetadata]:
        if revision:
            url = self._build_clientgateway_url(str(self.share['id']), 'virtualfiles', path.identifier, 'history')
        else:
            url = self._build_clientgateway_url(str(self.share['id']), 'virtualfiles', path.identifier)

        response = await self.make_request(
            'GET',
            url,
            expects=(200, 404,),
            throws=exceptions.MetadataError,
        )
        if response.status == 404:
            raise exceptions.NotFoundError(path)

        res = await response.json()

        if revision:
            try:
                res = next(x for x in res['Data'] if str(x['File']['Tick']) == revision)
            except StopIteration:
                raise exceptions.NotFoundError(str(path))
            return res['File'] if raw else RushFilesFileMetadata(res['File'], path)
        else:
            return res['Data'] if raw else RushFilesFileMetadata(res['Data'], path)

    def _search_inter_id(self,
                        res: dict,
                        child: str) -> Union[str, int, None]:
        for i, data in enumerate(res['Data']):
            if child == data['PublicName']:
                return data['InternalName'], i
        return None, None

    def _generate_uuid(self) -> str:
        uuid = str(uuid4())
        return uuid.replace('-', '')

    def _get_time_for_sending(self) -> str:
        return str(datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f%z'))
