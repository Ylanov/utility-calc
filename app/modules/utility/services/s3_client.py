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
        """Создает бакет, если его еще нет."""
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            logger.info(f"Creating S3 bucket: {self.bucket}")
            self.s3.create_bucket(Bucket=self.bucket)

            # Делаем бакет приватным по умолчанию,
            # файлы будем отдавать только через сгенерированные ссылки

    def upload_file(self, file_path: str, object_name: str) -> bool:
        """Загружает файл в S3."""
        try:
            self.s3.upload_file(file_path, self.bucket, object_name)
            return True
        except ClientError as e:
            logger.error(f"S3 Upload Error: {e}")
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