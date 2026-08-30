"""
shared/asset_storage/__init__.py

Single shared interface for all CinemaPilot agents to upload and access
binary assets (generated storyboard images, audio tracks, script PDFs) in GCS.

Rules:
  - Binary assets must never be stored in BigQuery; store them in GCS and
    record only the ``gs://`` URI in the Production Graph.
  - Authentication uses Application Default Credentials (ADC) exclusively.
  - All errors are wrapped in ``AssetStorageError``.

Bucket:
  ``gs://cinemapilot-2026-assets``
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from google.api_core.exceptions import GoogleAPIError
from google.cloud import storage


class AssetStorageError(Exception):
    """
    Raised when any Cloud Storage upload or URL generation operation fails.
    """


class AssetStorageClient:
    """
    Shared Cloud Storage client for CinemaPilot generated media assets.

    Authenticates exclusively via Application Default Credentials (ADC).
    """

    PROJECT: str = "cinemapilot-2026"
    BUCKET_NAME: str = "cinemapilot-2026-assets"

    def __init__(self) -> None:
        """
        Initialise the Cloud Storage client using Application Default Credentials.

        Raises:
            AssetStorageError: If client initialisation fails.
        """
        try:
            self._gcs = storage.Client(project=self.PROJECT)
        except Exception as exc:
            raise AssetStorageError(
                f"Failed to initialise Cloud Storage client. "
                f"Ensure ADC is configured (`gcloud auth application-default login`). "
                f"Underlying error: {exc}"
            ) from exc

    def upload_asset(
        self,
        entity_type: str,
        entity_id: str,
        asset_bytes: bytes,
        content_type: str = "image/png",
        extension: str = "png",
    ) -> str:
        """
        Upload binary asset bytes to GCS under a structured path.

        Path format: ``{entity_type}/{entity_id}/{uuid4}.{extension}``

        Args:
            entity_type: Type of entity associated with asset (e.g. "storyboard", "music").
            entity_id:   ID of entity (e.g. "scene_005").
            asset_bytes: Raw binary content to upload.
            content_type: MIME type of asset (default "image/png").
            extension:   File extension without leading dot (default "png").

        Returns:
            The full ``gs://`` URI string (e.g. "gs://cinemapilot-2026-assets/storyboard/scene_005/123.png").

        Raises:
            AssetStorageError: If upload to Cloud Storage fails.
        """
        clean_ext = extension.lstrip(".")
        blob_path = f"{entity_type}/{entity_id}/{uuid.uuid4().hex}.{clean_ext}"

        try:
            bucket = self._gcs.bucket(self.BUCKET_NAME)
            blob = bucket.blob(blob_path)
            blob.upload_from_string(asset_bytes, content_type=content_type)
            return f"gs://{self.BUCKET_NAME}/{blob_path}"
        except GoogleAPIError as exc:
            raise AssetStorageError(f"Cloud Storage upload failed: {exc}") from exc
        except Exception as exc:
            raise AssetStorageError(f"Unexpected error during asset upload: {exc}") from exc

    def download_asset_bytes(self, entity_type: str, entity_id: str, filename: str) -> tuple[bytes, str]:
        """
        Download binary asset bytes directly from GCS via storage.objectViewer.

        Args:
            entity_type: Validated asset category ('storyboard' | 'music').
            entity_id:   ID of entity (e.g. 'scene_005').
            filename:    File name (e.g. 'abc.png' or 'xyz.mp3').

        Returns:
            Tuple of (raw_bytes, content_type_str).

        Raises:
            AssetStorageError: If download fails or object is not found.
        """
        blob_path = f"{entity_type}/{entity_id}/{filename}"
        try:
            bucket = self._gcs.bucket(self.BUCKET_NAME)
            blob = bucket.blob(blob_path)
            if not blob.exists():
                raise AssetStorageError(f"Asset '{blob_path}' not found in bucket '{self.BUCKET_NAME}'.")
            data = blob.download_as_bytes()
            content_type = blob.content_type
            if not content_type:
                if filename.endswith(".png"):
                    content_type = "image/png"
                elif filename.endswith(".jpg") or filename.endswith(".jpeg"):
                    content_type = "image/jpeg"
                elif filename.endswith(".mp3"):
                    content_type = "audio/mpeg"
                elif filename.endswith(".wav"):
                    content_type = "audio/wav"
                elif filename.endswith(".mp4"):
                    content_type = "video/mp4"
                else:
                    content_type = "application/octet-stream"
            return data, content_type
        except AssetStorageError:
            raise
        except GoogleAPIError as exc:
            raise AssetStorageError(f"Cloud Storage download failed: {exc}") from exc
        except Exception as exc:
            raise AssetStorageError(f"Unexpected error downloading asset: {exc}") from exc

    def download_gs_uri(self, gs_uri: str) -> tuple[bytes, str]:
        """Download an existing ``gs://`` asset without reconstructing its path.

        Trailer assembly needs storyboard and music records as they already
        exist in the graph, while the dashboard media proxy uses structured
        paths.  Both routes stay inside this one storage client.
        """
        if not gs_uri.startswith("gs://"):
            raise AssetStorageError(f"Invalid GCS URI '{gs_uri}'. Expected 'gs://...'")
        parts = gs_uri[5:].split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise AssetStorageError(f"Invalid GCS URI format '{gs_uri}'.")

        bucket_name, blob_path = parts
        try:
            bucket = self._gcs.bucket(bucket_name)
            blob = bucket.blob(blob_path)
            if not blob.exists():
                raise AssetStorageError(f"Asset '{blob_path}' not found in bucket '{bucket_name}'.")
            data = blob.download_as_bytes()
            content_type = blob.content_type
            if not content_type:
                filename = blob_path.lower()
                if filename.endswith(".png"):
                    content_type = "image/png"
                elif filename.endswith((".jpg", ".jpeg")):
                    content_type = "image/jpeg"
                elif filename.endswith(".mp3"):
                    content_type = "audio/mpeg"
                elif filename.endswith(".wav"):
                    content_type = "audio/wav"
                elif filename.endswith(".mp4"):
                    content_type = "video/mp4"
                else:
                    content_type = "application/octet-stream"
            return data, content_type
        except AssetStorageError:
            raise
        except GoogleAPIError as exc:
            raise AssetStorageError(f"Cloud Storage download failed: {exc}") from exc
        except Exception as exc:
            raise AssetStorageError(f"Unexpected error downloading asset: {exc}") from exc

    SERVICE_ACCOUNT_EMAIL: str = "cinemapilot-agent@cinemapilot-2026.iam.gserviceaccount.com"

    def get_signed_url(self, gs_uri: str, expiration_minutes: int = 60) -> str:
        """
        Generate a temporary HTTP V4 signed URL for browser/dashboard access to a ``gs://`` URI.

        Uses IAM impersonation when running under user ADC credentials.

        Args:
            gs_uri: Full ``gs://`` URI string (e.g. "gs://cinemapilot-2026-assets/...").
            expiration_minutes: Validity duration in minutes (default 60).

        Returns:
            HTTPS signed URL string.

        Raises:
            AssetStorageError: If URL signing fails or URI format is invalid.
        """
        if not gs_uri.startswith("gs://"):
            raise AssetStorageError(f"Invalid GCS URI '{gs_uri}'. Expected 'gs://...'")

        path_parts = gs_uri[5:].split("/", 1)
        if len(path_parts) != 2:
            raise AssetStorageError(f"Invalid GCS URI format '{gs_uri}'.")

        bucket_name, blob_path = path_parts[0], path_parts[1]

        try:
            bucket = self._gcs.bucket(bucket_name)
            blob = bucket.blob(blob_path)

            # Direct V4 signature generation (works if client has private key or SA attached)
            try:
                return blob.generate_signed_url(
                    version="v4",
                    expiration=datetime.timedelta(minutes=expiration_minutes),
                    method="GET",
                )
            except AttributeError:
                # User ADC credentials lack private key. Impersonate service account via IAM Credentials API.
                from google.auth import impersonated_credentials
                from google.auth.transport.requests import Request

                source_creds = self._gcs._credentials
                if hasattr(source_creds, "refresh") and not source_creds.valid:
                    source_creds.refresh(Request())

                impersonated = impersonated_credentials.Credentials(
                    source_credentials=source_creds,
                    target_principal=self.SERVICE_ACCOUNT_EMAIL,
                    target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
                    lifetime=3600,
                )
                impersonated.refresh(Request())

                return blob.generate_signed_url(
                    version="v4",
                    expiration=datetime.timedelta(minutes=expiration_minutes),
                    method="GET",
                    service_account_email=self.SERVICE_ACCOUNT_EMAIL,
                    access_token=impersonated.token,
                )

        except GoogleAPIError as exc:
            raise AssetStorageError(f"Failed to generate signed URL: {exc}") from exc
        except Exception as exc:
            raise AssetStorageError(f"Failed to generate signed URL: {exc}") from exc
