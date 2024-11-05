#  * Copyright (c) 2020-2021. Authors: see NOTICE file.
#  *
#  * Licensed under the Apache License, Version 2.0 (the "License");
#  * you may not use this file except in compliance with the License.
#  * You may obtain a copy of the License at
#  *
#  *      http://www.apache.org/licenses/LICENSE-2.0
#  *
#  * Unless required by applicable law or agreed to in writing, software
#  * distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

import logging
import os
import shutil
from base64 import b64decode
from tempfile import TemporaryDirectory
from typing import List, Optional, Tuple

from bigpicture_metadata_interface import BPInterface
from celery import group, signature
from celery.result import allow_join_result
from crypt4gh_fsspec import Crypt4GHFileSystem
from cytomine.cytomine import Cytomine
from cytomine.models import (
    AbstractImage,
    ProjectCollection,
    PropertyCollection,
    UploadedFile,
)
from nacl.public import PrivateKey
from nacl.secret import SecretBox

from pims.api.exceptions import (
    BadRequestException,
    FilepathNotFoundProblem,
    NoMatchingFormatProblem,
)
from pims.api.utils.models import HistogramType
from pims.config import get_settings
from pims.files.archive import Archive, ArchiveError
from pims.files.file import (
    EXTRACTED_DIR,
    HISTOGRAM_STEM,
    ORIGINAL_STEM,
    PROCESSED_DIR,
    SPATIAL_STEM,
    UPLOAD_DIR_PREFIX,
    Path,
)
from pims.files.histogram import Histogram
from pims.files.image import Image
from pims.formats import AbstractFormat
from pims.formats.utils.factories import (
    ImportableFormatFactory,
    SpatialReadableFormatFactory,
)
from pims.importer.listeners import (
    CytomineListener,
    ImportEventType,
    ImportListener,
    StdoutListener,
)
from pims.processing.metadata import BPMetadataParser
from pims.processing.histograms.utils import build_histogram_file
from pims.tasks.queue import (
    BG_TASK_MAPPING,
    CELERY_TASK_MAPPING,
    Task,
    func_from_str,
)
from pims.utils.strings import unique_name_generator

log = logging.getLogger("pims.app")

FILE_ROOT_PATH = Path(get_settings().root)
PENDING_PATH = Path(get_settings().pending_path)
WRITING_PATH = Path(get_settings().writing_path)


class FileErrorProblem(BadRequestException):
    pass


class ImageParsingProblem(BadRequestException):
    pass


class FormatConversionProblem(BadRequestException):
    pass


class FileImporter:
    """
    Image importer from file. It moves a pending file to PIMS root path, tries to
    identify the file format, converts it if needed and checks its integrity.
    """

    listeners: List[ImportListener]

    # Pending file (not yet in `FILE_ROOT_PATH`)
    pending_file: Path
    pending_name: Optional[str]

    # Paths to directories for the current import (in `FILE_ROOT_PATH`)
    upload_dir: Optional[Path]
    processed_dir: Optional[Path]
    extracted_dir: Optional[Path]

    # Path to upload file (in `upload_dir`)
    upload_path: Optional[Path]

    # Original representation path (& image) (in `processed_dir`)
    original_path: Optional[Path]
    original: Optional[Image]

    # Spatial representation path (& image) (in `processed_dir`)
    spatial_path: Optional[Path]
    spatial: Optional[Image]

    # Histogram representation path (& histogram) (in `processed_dir`)
    histogram_path: Optional[Path]
    histogram: Optional[Histogram]

    def __init__(
        self, pending_file: Path, pending_name: Optional[str] = None,
        listeners: Optional[List[ImportListener]] = None
    ):
        """
        Parameters
        ----------
        pending_file
            A file to import from PENDING_PATH directory
        pending_name
            A name to use for the pending file.
            If not provided, the current pending file name is used.
        listeners
            A list of import listeners
        """
        self.listeners = listeners if listeners is not None else []
        self.pending_file = pending_file
        self.pending_name = pending_name

        self.upload_dir = None
        self.upload_path = None
        self.original_path = None
        self.original = None
        self.spatial_path = None
        self.spatial = None
        self.histogram_path = None
        self.histogram = None

        self.processed_dir = None
        self.extracted_dir = None

    def notify(self, method: ImportEventType, *args, **kwargs):
        for listener in self.listeners:
            try:
                getattr(listener, method)(*args, **kwargs)
            except AttributeError as e:
                log.error(e)
                log.warning(f"No method {method} for import listener {listener}")

    def run(self, prefer_copy: bool = False):
        """
        Import the pending file. It moves a pending file to PIMS root path, tries to
        identify the file format, converts it if needed and checks its integrity.

        Parameters
        ----------
        prefer_copy : bool
            Prefer copy the pending file instead of moving it. Useful for tests.

        Raises
        ------
        FilepathNotFoundProblem
            If pending file is not found.
        """
        try:
            self.notify(ImportEventType.START_DATA_EXTRACTION, self.pending_file)

            # Check the file is in pending area,
            # or comes from a extracted collection
            if (not self.pending_file.is_extracted() and
                (self.pending_file.parent != WRITING_PATH and self.pending_file.parent != PENDING_PATH)) \
                    or not self.pending_file.exists():
                self.notify(ImportEventType.FILE_NOT_FOUND, self.pending_file)
                raise FilepathNotFoundProblem(self.pending_file)

            # Move the file to PIMS root path
            upload_dir_name = Path(
                f"{UPLOAD_DIR_PREFIX}"
                f"{str(unique_name_generator())}"
            )
            self.upload_dir = FILE_ROOT_PATH / upload_dir_name
            self.mkdir(self.upload_dir)

            if self.pending_name:
                name = self.pending_name
            else:
                name = self.pending_file.name
            self.upload_path = self.upload_dir / name

            self.move(self.pending_file, self.upload_path, prefer_copy)

            # If the pending file comes from an archive
            if not prefer_copy and self.pending_file.is_extracted():
                # Create symlink in processed to keep track of parent archive
                self.mksymlink(self.pending_file, self.upload_path)

            self.notify(
                ImportEventType.MOVED_PENDING_FILE,
                self.pending_file, self.upload_path
            )
            self.notify(ImportEventType.END_DATA_EXTRACTION, self.upload_path)

            # Identify format
            self.notify(ImportEventType.START_FORMAT_DETECTION, self.upload_path)

            format_factory = ImportableFormatFactory()
            format = format_factory.match(self.upload_path)
            archive = None
            if format is None:
                archive = Archive.from_path(self.upload_path)
                if archive:
                    format = archive.format

            if format is None:
                self.notify(ImportEventType.ERROR_NO_FORMAT, self.upload_path)
                raise NoMatchingFormatProblem(self.upload_path)
            self.notify(
                ImportEventType.END_FORMAT_DETECTION,
                self.upload_path, format
            )

            # Create processed dir
            self.processed_dir = self.upload_dir / Path(PROCESSED_DIR)
            self.mkdir(self.processed_dir)

            # Create original role
            original_filename = Path(
                f"{ORIGINAL_STEM}.{format.get_identifier()}"
            )
            self.original_path = self.processed_dir / original_filename
            if archive:
                try:
                    self.notify(
                        ImportEventType.START_UNPACKING, self.upload_path
                    )
                    archive.extract(self.original_path)
                except ArchiveError as e:
                    self.notify(
                        ImportEventType.ERROR_UNPACKING, self.upload_path,
                        exception=e
                    )
                    raise FileErrorProblem(self.upload_path)

                # Now the archive is extracted, check if it's a multi-file format
                format = format_factory.match(self.original_path)
                if format:
                    # It is a multi-file format
                    original_filename = Path(
                        f"{ORIGINAL_STEM}.{format.get_identifier()}"
                    )
                    new_original_path = self.processed_dir / original_filename
                    self.move(self.original_path, new_original_path)
                    self.original_path = new_original_path
                    format = format.__class__(self.original_path)

                    self.notify(
                        ImportEventType.END_UNPACKING, self.upload_path,
                        self.original_path, format=format, is_collection=False
                    )
                    self.upload_path = self.original_path
                else:
                    self.extracted_dir = self.processed_dir / Path(EXTRACTED_DIR)
                    self.mksymlink(self.extracted_dir, self.original_path)

                    collection = self.import_collection(
                        self.original_path, prefer_copy
                    )
                    self.notify(
                        ImportEventType.END_UNPACKING, self.upload_path,
                        self.original_path, is_collection=True
                    )
                    return collection
            else:
                self.mksymlink(self.original_path, self.upload_path)
                assert self.original_path.has_original_role()

            # Check original image integrity
            self.notify(ImportEventType.START_INTEGRITY_CHECK, self.original_path)
            self.original = Image(self.original_path, format=format)
            errors = self.original.check_integrity(check_metadata=True)
            if len(errors) > 0:
                self.notify(
                    ImportEventType.ERROR_INTEGRITY_CHECK, self.original_path,
                    integrity_errors=errors
                )
                raise ImageParsingProblem(self.original)
            self.notify(ImportEventType.END_INTEGRITY_CHECK, self.original)

            if format.is_spatial():
                self.deploy_spatial(format)
            else:
                raise NotImplementedError()

            self.deploy_histogram(self.original.get_spatial())

            # Finished
            self.notify(
                ImportEventType.END_SUCCESSFUL_IMPORT,
                self.upload_path, self.original
            )
            return [self.upload_path]
        except Exception as e:
            self.notify(
                ImportEventType.FILE_ERROR,
                self.upload_path, exeception=e
            )
            raise e

    def deploy_spatial(self, format: AbstractFormat) -> Image:
        """
        Deploy a spatial representation of the image so that it can be used for
        efficient spatial requests.
        """
        self.notify(ImportEventType.START_SPATIAL_DEPLOY, self.original_path)
        if format.need_conversion:
            # Do the spatial conversion
            try:
                ext = format.conversion_format().get_identifier()
                spatial_filename = Path(f"{SPATIAL_STEM}.{ext}")
                self.spatial_path = self.processed_dir / spatial_filename
                self.notify(
                    ImportEventType.START_CONVERSION,
                    self.spatial_path, self.upload_path
                )

                r = format.convert(self.spatial_path)
                if not r or not self.spatial_path.exists():
                    self.notify(
                        ImportEventType.ERROR_CONVERSION,
                        self.spatial_path
                    )
                    raise FormatConversionProblem()
            except Exception as e:
                self.notify(
                    ImportEventType.ERROR_CONVERSION,
                    self.spatial_path, exception=e
                )
                raise FormatConversionProblem()

            self.notify(ImportEventType.END_CONVERSION, self.spatial_path)

            # Check format of converted file
            self.notify(ImportEventType.START_FORMAT_DETECTION, self.spatial_path)
            spatial_format = SpatialReadableFormatFactory().match(self.spatial_path)
            if not spatial_format:
                self.notify(ImportEventType.ERROR_NO_FORMAT, self.spatial_path)
                raise NoMatchingFormatProblem(self.spatial_path)
            self.notify(
                ImportEventType.END_FORMAT_DETECTION,
                self.spatial_path, spatial_format
            )

            self.spatial = Image(self.spatial_path, format=spatial_format)

            # Check spatial image integrity
            self.notify(ImportEventType.START_INTEGRITY_CHECK, self.spatial_path)
            errors = self.spatial.check_integrity(check_metadata=True)
            if len(errors) > 0:
                self.notify(
                    ImportEventType.ERROR_INTEGRITY_CHECK, self.spatial_path,
                    integrity_errors=errors
                )
                raise ImageParsingProblem(self.spatial)
            self.notify(ImportEventType.END_INTEGRITY_CHECK, self.spatial)

        else:
            # Create spatial role
            spatial_filename = Path(f"{SPATIAL_STEM}.{format.get_identifier()}")
            self.spatial_path = self.processed_dir / spatial_filename
            self.mksymlink(self.spatial_path, self.original_path)
            self.spatial = Image(self.spatial_path, format=format)

        assert self.spatial.has_spatial_role()
        self.notify(ImportEventType.END_SPATIAL_DEPLOY, self.spatial)
        return self.spatial

    def deploy_histogram(self, image: Image) -> Histogram:
        """
        Deploy an histogram representation of the image so that it can be used for
        efficient histogram requests.
        """
        self.histogram_path = self.processed_dir / Path(HISTOGRAM_STEM)
        self.notify(
            ImportEventType.START_HISTOGRAM_DEPLOY,
            self.histogram_path, image
        )
        try:
            self.histogram = build_histogram_file(
                image, self.histogram_path, HistogramType.FAST
            )
        except (FileNotFoundError, FileExistsError) as e:
            self.notify(
                ImportEventType.ERROR_HISTOGRAM, self.histogram_path, image,
                exception=e
            )
            raise FileErrorProblem(self.histogram_path)

        assert self.histogram.has_histogram_role()
        self.notify(
            ImportEventType.END_HISTOGRAM_DEPLOY, self.histogram_path, image
        )
        return self.histogram

    def mkdir(self, directory: Path):
        """Make a directory (with notifications)"""
        try:
            directory.mkdir()  # TODO: mode
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_ERROR, directory, exception=e)
            raise FileErrorProblem(directory)

    def move(self, origin: Path, dest: Path, prefer_copy: bool = False):
        """Move origin to dest (with notifications)"""
        try:
            if prefer_copy:
                shutil.copy(origin, dest)
            else:
                shutil.move(origin, dest)
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_NOT_MOVED, origin, exception=e)
            raise FileErrorProblem(origin)

    def mksymlink(self, path: Path, target: Path):
        """Make a symlink from path to target (with notifications)"""
        try:
            path.symlink_to(
                target,
                target_is_directory=target.is_dir()
            )
        except (FileNotFoundError, FileExistsError, OSError) as e:
            self.notify(ImportEventType.FILE_ERROR, path, exception=e)
            raise FileErrorProblem(path)

    def import_collection(self, collection: Path, prefer_copy: bool = False):
        """Import recursively children of the collection."""
        cytomine = None
        for listener in self.listeners:
            if isinstance(listener, CytomineListener):
                cytomine = listener
                break
        if cytomine:
            task = Task.IMPORT_WITH_CYTOMINE
        else:
            task = Task.IMPORT

        imported = list()
        format_factory = ImportableFormatFactory()
        tasks = list()
        # Collection children are extracted recursively into collection
        # directories, until the directory is an image format (we can thus have
        # multi-file formats as directories in a collection).
        for child in collection.get_extracted_children(
                stop_recursion_cond=format_factory.match
        ):
            self.notify(
                ImportEventType.REGISTER_FILE, child, self.upload_path
            )
            try:
                if cytomine:
                    new_listener = cytomine.new_listener_from_registered_child(child)
                    args = [
                        new_listener.auth, str(child), child.name,
                        new_listener, prefer_copy
                    ]
                else:
                    args = [str(child), child.name, prefer_copy]
                tasks.append((task, args))
            except Exception as _:  # noqa
                # Do not propagate error to siblings
                # Each importer is independent
                pass

        def _sequential_imports():
            for name, args_ in tasks:
                func_from_str(BG_TASK_MAPPING.get(name))(*args_)

        if not get_settings().task_queue_enabled:
            _sequential_imports()
        else:
            try:
                task_group = group([
                    signature(CELERY_TASK_MAPPING.get(name), args_)
                    for name, args_ in tasks
                ])
                # WARNING !
                # These tasks are synchronous with respect to the parent task (the archive)
                # It is required to update the parent (the archive) status when everything is
                # finished. Code should be refactored to use Celery callbacks but it does not
                # seem so easy.
                # Current implementation may cause deadlock if the worker pool is exhausted,
                # while the parent task is waiting for subtasks to finish.
                # http://docs.celeryq.org/en/latest/userguide/tasks.html#task-synchronous-subtasks
                with allow_join_result():
                    r = task_group.apply_async()
                    r.get()  # Wait for group to finish
            except Exception as e:  # noqa
                # WARNING !
                # Catch too many exceptions such as those related to import logic ?
                # TODO: identify Celery exception raised when trying to use it while rabbitmq is
                #  down
                # However, it should not happen in production.
                _sequential_imports()

        return imported

    def import_from_path(self) -> Path:
        """Import a file from a given path."""

        try:
            self.notify(ImportEventType.START_DATA_EXTRACTION, self.pending_file)

            upload_dir_name = Path(
                f"{UPLOAD_DIR_PREFIX}"
                f"{str(unique_name_generator())}"
            )
            self.upload_dir = FILE_ROOT_PATH / upload_dir_name
            self.mkdir(self.upload_dir)

            if self.pending_name:
                name = self.pending_name
            else:
                name = self.pending_file.name
            self.upload_path = self.upload_dir / name

            self.mksymlink(self.upload_path, self.pending_file)

            self.notify(
                ImportEventType.MOVED_PENDING_FILE,
                self.pending_file,
                self.upload_path,
            )
            self.notify(ImportEventType.END_DATA_EXTRACTION, self.upload_path)

            self.notify(ImportEventType.START_FORMAT_DETECTION, self.upload_path)

            format_factory = ImportableFormatFactory()
            format = format_factory.match(self.upload_path)

            if format is None:
                self.notify(ImportEventType.ERROR_NO_FORMAT, self.upload_path)
                raise NoMatchingFormatProblem(self.upload_path)
            self.notify(
                ImportEventType.END_FORMAT_DETECTION,
                self.upload_path, format
            )

            self.processed_dir = self.upload_dir / Path(PROCESSED_DIR)
            self.mkdir(self.processed_dir)

            original_filename = Path(f"{ORIGINAL_STEM}.{format.get_identifier()}")
            self.original_path = self.processed_dir / original_filename
   
            self.mksymlink(self.original_path, self.upload_path)
            assert self.original_path.has_original_role()

            self.notify(ImportEventType.START_INTEGRITY_CHECK, self.original_path)
            self.original = Image(self.original_path, format=format)
            errors = self.original.check_integrity(check_metadata=True)
            if len(errors) > 0:
                self.notify(
                    ImportEventType.ERROR_INTEGRITY_CHECK,
                    self.original_path,
                    integrity_errors=errors,
                )
                raise ImageParsingProblem(self.original)
            self.notify(ImportEventType.END_INTEGRITY_CHECK, self.original)

            if not format.is_spatial():
                raise NotImplementedError()

            self.deploy_spatial(format)

            self.deploy_histogram(self.original.get_spatial())

            self.notify(
                ImportEventType.END_SUCCESSFUL_IMPORT,
                self.upload_path,
                self.original,
            )
            return self.upload_path
        except Exception as e:
            self.notify(
                ImportEventType.FILE_ERROR,
                self.upload_path,
                exception=e,
            )
            raise e


def run_import(
    filepath: str,
    name: str,
    extra_listeners: Optional[List[ImportListener]] = None,
    prefer_copy: bool = False,
):
    pending_file = Path(filepath)

    if extra_listeners is not None:
        if not type(extra_listeners) is list:
            extra_listeners = list(extra_listeners)
    else:
        extra_listeners = []

    listeners = [StdoutListener(name)] + extra_listeners
    fi = FileImporter(pending_file, name, listeners)
    fi.run(prefer_copy)


def get_folder_size(folder_path) -> int:
    """Get the total size in bytes of a folder."""
    total_size = 0
    for dirpath, _, filenames in os.walk(folder_path):
        for file in filenames:
            file_path = os.path.join(dirpath, file)
            total_size += os.path.getsize(file_path)

    return total_size


def run_import_from_path(
    dataset_path: str,
    cytomine_auth: Tuple[str, str, str],
    storage_id: int,
    image_server_id: int,
    user_id: int,
) -> List[UploadedFile]:
    """Run importer from a given path."""

    uploaded_files = []
    images_path = Path(os.path.join(dataset_path, "images"))
    for item in images_path.iterdir():
        if not item.is_dir():
            continue

        image_path = os.path.join(images_path, item)

        uf = UploadedFile(
            original_filename=item.name,
            filename=image_path,
            size=get_folder_size(image_path),
            ext="",
            content_type="",
            id_storage=storage_id,
            id_user=user_id,
            id_image_server=image_server_id,
            status=UploadedFile.UPLOADED,
        )

        listeners = [
            StdoutListener(item.name),
            CytomineListener(
                cytomine_auth,
                uf,
                projects=ProjectCollection(),
                user_properties=iter([]),
            ),
        ]

        fi = FileImporter(Path(image_path), item.name, listeners)
        fi.import_from_path()

        uploaded_files.append(uf)

    return uploaded_files


def import_metadata(metadata_path: str, uploaded_files: List[UploadedFile]) -> bool:
    """Import metadata from a given path."""

    abstract_images = []
    for uf in uploaded_files:
        data = Cytomine.get_instance().get(f"uploadedfile/{uf.id}/abstractimage.json")
        abstract_images.append(
            AbstractImage().populate(data)
        )

    files = [
        file
        for file in os.listdir(metadata_path)
        if os.path.isfile(os.path.join(metadata_path, file))
    ]
    settings = get_settings()
    fs = Crypt4GHFileSystem(
        decode_key(settings.crypt4gh_private_key),
    )

    with TemporaryDirectory() as tmp_dir:
        metadata_directory_path = os.path.join(tmp_dir, "METADATA")
        os.makedirs(metadata_directory_path, exist_ok=True)

        for file in files:
            with fs.open(os.path.join(metadata_path, file), "rb") as fp:
                decrypted_data = fp.read()

            with open(os.path.join(metadata_directory_path, file[:-5]), "wb") as fp:
                fp.write(decrypted_data)

        if not BPInterface.validate(tmp_dir):
            return False

        studies, beings, datasets = BPInterface.parse_xml_files(tmp_dir)

    metadata_parser = BPMetadataParser(studies, beings, datasets)

    # Upload metadata file
    for ai in abstract_images:
        metadata = metadata_parser.parse({"image": ai.originalFilename})

        properties = PropertyCollection(ai)
        for key, value in metadata.items():
            properties.append(
                ai,
                f"MSMDAD.{key}",
                value,
            )

        properties.save()

    return True


def decode_key(key: str) -> PrivateKey:
    """Decode the key and extract the private key."""

    NACL_KEY_LENGTH = SecretBox.KEY_SIZE

    secret_key = b64decode(key)[-NACL_KEY_LENGTH:]

    if len(secret_key) != NACL_KEY_LENGTH:
        raise ValueError(f"The extracted key is not {NACL_KEY_LENGTH} bytes long!")

    return PrivateKey(secret_key)
