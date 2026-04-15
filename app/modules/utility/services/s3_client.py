import boto3
from botocore.exceptions import ClientError
from urllib.parse import urlparse, urlunparse
from app.core.config import settings
import logging

logger = logging.getLogger("s3_client")


class S3Service:
    def __init__(self):
        self.is_available = False
        self.s3 = None
        self.bucket = settings.S3_BUCKET_NAME
        try:
            self.s3 = boto3.client(
                's3',
                endpoint_url=settings.S3_ENDPOINT_URL,
                aws_access_key_id=settings.S3_ACCESS_KEY,
                aws_secret_access_key=settings.S3_SECRET_KEY,
                region_name="us-east-1"
            )
            self._ensure_bucket_exists()
            self.is_available = True
            logger.info("S3Service initialized successfully.")
        except Exception as e:
            logger.warning(
                f"S3Service unavailable (MinIO down?): {e}. "
                "PDF files will be served from local static directory as fallback."
            )

    def _ensure_bucket_exists(self):
        """Создает бакет, если его еще не существует."""
        try:
            self.s3.head_bucket(Bucket=self.bucket)
            logger.info(f"S3 bucket '{self.bucket}' already exists.")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code in ['404', 'NoSuchBucket']:
                logger.info(f"S3 bucket '{self.bucket}' not found. Creating it...")
                try:
                    self.s3.create_bucket(Bucket=self.bucket)
                    logger.info(f"S3 bucket '{self.bucket}' created successfully.")
                except ClientError as create_error:
                    if create_error.response.get("Error", {}).get("Code") != 'BucketAlreadyOwnedByYou':
                        logger.error(f"Failed to create S3 bucket: {create_error}")
                        raise create_error
            else:
                logger.error(f"Unexpected S3 error when checking bucket: {e}")
                raise e

    def upload_file(self, file_path: str, object_name: str) -> bool:
        """Загружает файл в S3."""
        if not self.is_available:
            return False
        try:
            self.s3.upload_file(file_path, self.bucket, object_name)
            return True
        except ClientError as e:
            logger.error(f"S3 Upload Error: {e}")
            return False

    def upload_fileobj(self, file_obj, object_name: str) -> bool:
        """Загружает файловый объект (UploadFile.file) прямо в S3 без сохранения на диск."""
        if not self.is_available:
            return False
        try:
            self.s3.upload_fileobj(file_obj, self.bucket, object_name)
            return True
        except ClientError as e:
            logger.error(f"S3 Upload FileObj Error: {e}")
            return False

    def get_presigned_url(self, object_name: str, expiration=300) -> str:
        """
        Генерирует временную ссылку на скачивание файла (по умолчанию живет 5 минут).
        """
        if not self.is_available:
            return None
        try:
            url = self.s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket, 'Key': object_name},
                ExpiresIn=expiration
            )
            # Заменяем внутренний хост докера на публичный, используя urlparse
            # (безопасно в отличие от str.replace, не затронет path/query содержащие endpoint)
            if settings.S3_PUBLIC_URL:
                parsed = urlparse(url)
                public = urlparse(settings.S3_PUBLIC_URL)
                url = urlunparse(parsed._replace(scheme=public.scheme, netloc=public.netloc))
            return url
        except ClientError as e:
            logger.error(f"S3 Presigned URL Error: {e}")
            return None


s3_service = S3Service()
