"""
Custom Cloudinary storage backends.

Default storage (MediaCloudinaryStorage) stays public — fine for
non-sensitive assets if/when we have any (e.g. provider logos).

KYCDocumentStorage is private + auto resource_type, for anything
in the KYC/document trust boundary: CustomerKYC.id_document,
CustomerKYC.selfie, PartnerKYC.cac_certificate,
PartnerKYC.naicom_licence_doc, PartnerKYC.proof_of_address,
SubscriptionDocument.file.
"""

import cloudinary.uploader
import cloudinary.utils
from cloudinary_storage.storage import MediaCloudinaryStorage


class KYCDocumentStorage(MediaCloudinaryStorage):
    """
    Forces resource_type='auto' (so PDFs, images, whatever a KYC upload
    turns out to be all land correctly without per-field guessing) and
    type='private' (so the asset is NOT publicly reachable by a guessed
    or scraped URL — only via a signed, time-limited URL we generate).
    """

    def _upload(self, name, content):
        options = {
            "use_filename": True,
            "unique_filename": True,
            "resource_type": "auto",
            "type": "private",
            "folder": getattr(self, "TAG", None),
        }
        return cloudinary.uploader.upload(content, **options)

    def url(self, name, **kwargs):
        """
        Generate a signed delivery URL. Private/authenticated Cloudinary
        assets are NOT servable via the plain public URL pattern, so we
        can't rely on the parent class's url() here.

        expires_at: pass a unix timestamp if you want a short-lived link
        (e.g. for displaying a KYC doc to a staff reviewer for 10 minutes)
        instead of an indefinitely valid one.
        """
        url, _ = cloudinary.utils.cloudinary_url(
            name,
            resource_type="auto",
            type="private",
            sign_url=True,
            **kwargs,
        )
        return url
