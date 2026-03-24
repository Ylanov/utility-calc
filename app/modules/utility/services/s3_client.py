import boto3
from botocore.exceptions import ClientError
from app.core.config import settings
import logging

logger = logging.getLogger("s3_client")


class S3Service:
    def __init__(self):
        self.s3 = boto3.client(
            's3',
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name="us-east-1"
        )
        self.bucket = settings.S3_BUCKET_NAME
        self._ensure_bucket_exists()

    def _ensure_bucket_exists(self):
        """Создает бакет, если его еще не существует."""
        try:
            self.s3.head_bucket(Bucket=self.bucket)
            logger.info(f"S3 bucket '{self.bucket}' already exists.")
        except ClientError as e:
            # Проверяем код ошибки. Если это '404' (или 'NoSuchBucket'), значит бакета нет.
            error_code = e.response.get("Error", {}).get("Code")
            if error_code in ['404', 'NoSuchBucket']:
                logger.info(f"S3 bucket '{self.bucket}' not found. Creating it...")
                try:
                    self.s3.create_bucket(Bucket=self.bucket)
                    logger.info(f"S3 bucket '{self.bucket}' created successfully.")
                except ClientError as create_error:
                    # Обрабатываем возможную ошибку, если другой процесс создал бакет за эту миллисекунду
                    if create_error.response.get("Error", {}).get("Code") != 'BucketAlreadyOwnedByYou':
                        logger.error(f"Failed to create S3 bucket: {create_error}")
                        raise create_error
            else:
                # Если ошибка другая (например, нет доступа), мы должны ее увидеть
                logger.error(f"Unexpected S3 error when checking bucket: {e}")
                raise e

    def upload_file(self, file_path: str, object_name: str) -> bool:
        """Загружает файл в S3."""
        try:
            self.s3.upload_file(file_path, self.bucket, object_name)
            return True
        except ClientError as e:
            logger.error(f"S3 Upload Error: {e}")
            return False

    def upload_fileobj(self, file_obj, object_name: str) -> bool:
        """Загружает файловый объект (UploadFile.file) прямо в S3 без сохранения на диск."""
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
        try:
            url = self.s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket, 'Key': object_name},
                ExpiresIn=expiration
            )
            # Если мы отдаем ссылку клиенту в браузер, заменяем внутренний хост докера на публичный
            if settings.S3_PUBLIC_URL:
                url = url.replace(settings.S3_ENDPOINT_URL, settings.S3_PUBLIC_URL)
            return url
        except ClientError as e:
            logger.error(f"S3 Presigned URL Error: {e}")
            return None


s3_service = S3Service()
