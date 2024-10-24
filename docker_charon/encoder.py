from __future__ import annotations

import sys
from pathlib import Path
from typing import IO, Iterator, Optional, Union
from zipfile import ZipFile

from dxf import DXF, DXFBase
from tqdm import tqdm

from docker_charon.common import (
    PYDANTIC_V2,
    Authenticator,
    Blob,
    BlobLocationInRegistry,
    BlobPathInZip,
    Manifest,
    PayloadDescriptor,
    PayloadSide,
    progress_as_string,
)


def add_blobs_to_zip(
    dxf_base: DXFBase,
    zip_file: ZipFile,
    blobs_to_pull: list[Blob],
    blobs_already_transferred: list[Blob],
) -> dict[str, Union[BlobPathInZip, BlobLocationInRegistry]]:
    blobs_paths = {}
    for blob_index, blob in enumerate(blobs_to_pull):
        print(progress_as_string(blob_index, blobs_to_pull), end=" ", file=sys.stderr)
        if blob.digest in blobs_paths:
            print(
                f"Skipping {blob} because it's in {blobs_paths[blob.digest]}",
                file=sys.stderr,
            )
            continue

        if dest_blob := get_blob_with_same_digest(
            blobs_already_transferred, blob.digest
        ):
            print(
                f"Skipping {blob} because it's already in the destination registry "
                f"in the repository {dest_blob.repository}",
                file=sys.stderr,
            )
            blobs_paths[blob.digest] = BlobLocationInRegistry(
                repository=dest_blob.repository
            )
            continue

        # nominal case
        print(f"Pulling blob {blob} and storing it in the zip", file=sys.stderr)
        blob_path_in_zip = download_blob_to_zip(dxf_base, blob, zip_file)
        blobs_paths[blob.digest] = BlobPathInZip(zip_path=blob_path_in_zip)
    return blobs_paths


def download_blob_to_zip(dxf_base: DXFBase, blob: Blob, zip_file: ZipFile):
    repository_dxf = DXF.from_base(dxf_base, blob.repository)
    bytes_iterator, total_size = repository_dxf.pull_blob(blob.digest, size=True)

    # we write the blob directly to the zip file
    with tqdm(total=total_size, unit="B", unit_scale=True) as pbar:
        blob_path_in_zip = f"blobs/{blob.digest}"
        with zip_file.open(blob_path_in_zip, "w", force_zip64=True) as blob_in_zip:
            for chunk in bytes_iterator:
                blob_in_zip.write(chunk)
                pbar.update(len(chunk))
    return blob_path_in_zip


def get_blob_with_same_digest(list_of_blobs: list[Blob], digest: str) -> Optional[Blob]:
    for blob in list_of_blobs:
        if blob.digest == digest:
            return blob


def get_manifest_and_list_of_blobs_to_pull(
    dxf_base: DXFBase, docker_image: str
) -> tuple[Manifest, list[Blob]]:
    manifest = Manifest(dxf_base, docker_image, PayloadSide.ENCODER)
    return manifest, manifest.get_list_of_blobs()


def get_manifests_and_list_of_all_blobs(
    dxf_base: DXFBase, docker_images: Iterator[str]
) -> tuple[list[Manifest], list[Blob]]:
    manifests = []
    blobs_to_pull = []
    for docker_image in docker_images:
        manifest, blobs = get_manifest_and_list_of_blobs_to_pull(dxf_base, docker_image)
        manifests.append(manifest)
        blobs_to_pull += blobs
    return manifests, blobs_to_pull


def uniquify_blobs(blobs: list[Blob]) -> list[Blob]:
    result = []
    for blob in blobs:
        if blob.digest not in [x.digest for x in result]:
            result.append(blob)
    return result


def separate_images_to_transfer_and_images_to_skip(
    docker_images_to_transfer: list[str], docker_images_already_transferred: list[str]
) -> tuple[list[str], list[str]]:
    docker_images_to_transfer_with_blobs = []
    docker_images_to_skip = []
    for docker_image in docker_images_to_transfer:
        if docker_image not in docker_images_already_transferred:
            docker_images_to_transfer_with_blobs.append(docker_image)
        else:
            print(
                f"Skipping {docker_image} as it has already been transferred",
                file=sys.stderr,
            )
            docker_images_to_skip.append(docker_image)
    return docker_images_to_transfer_with_blobs, docker_images_to_skip


def create_zip_from_docker_images(
    dxf_base: DXFBase,
    docker_images_to_transfer: list[str],
    docker_images_already_transferred: list[str],
    zip_file: ZipFile,
) -> None:
    payload_descriptor = PayloadDescriptor.from_images(
        docker_images_to_transfer, docker_images_already_transferred
    )

    manifests, blobs_to_pull = get_manifests_and_list_of_all_blobs(
        dxf_base, payload_descriptor.get_images_not_transferred_yet()
    )
    _, blobs_already_transferred = get_manifests_and_list_of_all_blobs(
        dxf_base, docker_images_already_transferred
    )
    payload_descriptor.blobs_paths = add_blobs_to_zip(
        dxf_base, zip_file, blobs_to_pull, blobs_already_transferred
    )
    for manifest in manifests:
        dest = payload_descriptor.manifests_paths[manifest.docker_image_name]
        if "linux/amd64" in manifest.content:
            single_manifest = manifest.content["linux/amd64"]
        else:
            single_manifest = manifest.content
        zip_file.writestr(dest, single_manifest)
    if PYDANTIC_V2:
        payload_descriptor_json = payload_descriptor.model_dump_json(indent=4)
    else:
        payload_descriptor_json = payload_descriptor.json(indent=4)
    zip_file.writestr("payload_descriptor.json", payload_descriptor_json)


def make_payload(
    zip_file: Union[IO, Path, str],
    docker_images_to_transfer: list[str],
    docker_images_already_transferred: list[str] = [],
    registry: str = "registry-1.docker.io",
    secure: bool = True,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    """
    Creates a payload from a list of docker images
    All the docker images must be in the same registry.
    This is currently a limitation of the docker-charon package.

    If you are interested in multi-registries, please open an issue.

    # Arguments
        zip_file: The path to the zip file to create. It can be a `pathlib.Path` or
            a `str`. It's also possible to pass a file-like object. The payload with
            all the docker images is a single zip file.
        docker_images_to_transfer: The list of docker images to transfer. Do not include
            the registry name in the image name.
        docker_images_already_transferred: The list of docker images that have already
            been transferred to the air-gapped registry. Do not include the registry
            name in the image name.
        registry: the registry to push to. It defaults to `registry-1.docker.io` (dockerhub).
        secure: Set to `False` if the registry doesn't support HTTPS (TLS). Default
            is `True`.
        username: The username to use for authentication to the registry. Optional if
            the registry doesn't require authentication.
        password: The password to use for authentication to the registry. Optional if
            the registry doesn't require authentication.
    """
    authenticator = Authenticator(username, password)

    with DXFBase(
        host=registry, auth=authenticator.auth, insecure=not secure
    ) as dxf_base:
        with ZipFile(zip_file, "w") as zip_file_opened:
            create_zip_from_docker_images(
                dxf_base,
                docker_images_to_transfer,
                docker_images_already_transferred,
                zip_file_opened,
            )
