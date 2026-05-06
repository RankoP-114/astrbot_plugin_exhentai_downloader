import io
import os
import zipfile
from typing import Optional

from .log import logger


def pack_zip(
    image_paths: list[str],
    output_path: str,
    password: Optional[str] = None,
) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)

    if password:
        try:
            return _pack_zip_encrypted(image_paths, output_path, password)
        except ImportError:
            raise RuntimeError("ZIP 加密需要安装 pyzipper，请先安装依赖。") from None
        except Exception as e:
            if os.path.exists(output_path):
                os.remove(output_path)
            logger.error(f"Encrypted ZIP failed: {e}")
            raise
    return _pack_zip_standard(image_paths, output_path)


def _pack_zip_standard(image_paths: list[str], output_path: str) -> str:
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in image_paths:
            arcname = os.path.basename(path)
            zf.write(path, arcname)
    return output_path


def _pack_zip_encrypted(image_paths: list[str], output_path: str, password: str) -> str:
    import pyzipper
    with pyzipper.AESZipFile(output_path, "w", compression=pyzipper.ZIP_DEFLATED) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.setencryption(pyzipper.WZ_AES)
        for path in image_paths:
            arcname = os.path.basename(path)
            zf.write(path, arcname)
    return output_path


def pack_pdf(
    image_paths: list[str],
    output_path: str,
    password: Optional[str] = None,
) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if os.path.exists(output_path):
        os.remove(output_path)

    sorted_paths = sorted(image_paths, key=lambda p: os.path.basename(p))

    import img2pdf
    try:
        pdf_data = img2pdf.convert(sorted_paths)
        if password:
            try:
                _encrypt_pdf_bytes(pdf_data, output_path, password)
            except ImportError:
                raise RuntimeError("PDF 加密需要安装 pypdf，请先安装依赖。") from None
        else:
            with open(output_path, "wb") as f:
                f.write(pdf_data)
    except Exception as e:
        if os.path.exists(output_path):
            os.remove(output_path)
        logger.error(f"PDF packaging failed: {e}")
        raise
    return output_path


def _encrypt_pdf_bytes(pdf_data: bytes, output_path: str, password: str) -> str:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_data))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password, password, algorithm="AES-256")
    with open(output_path, "wb") as f:
        writer.write(f)
    return output_path
