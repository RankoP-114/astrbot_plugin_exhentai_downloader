from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Gallery:
    gid: int
    token: str
    title: str = ""
    title_jpn: str = ""
    category: str = ""
    thumb_url: str = ""
    url: str = ""
    tags: list[str] = field(default_factory=list)
    uploader: str = ""
    posted: str = ""
    filecount: int = 0
    filesize: int = 0
    rating: float = 0.0
    language: str = ""
    parent_gid: Optional[int] = None
    parent_key: Optional[str] = None

    @property
    def gallery_url(self) -> str:
        return f"https://e-hentai.org/g/{self.gid}/{self.token}/"

    @property
    def filesize_mb(self) -> float:
        return round(self.filesize / (1024 * 1024), 2)

    def format_info(self) -> str:
        lines = [
            f"📖 {self.title}",
            f"🆔 #{self.gid}",
            f"📁 {self.category}",
            f"🗂️ {self.filecount} 页 | {self.filesize_mb} MB",
            f"⭐ {self.rating:.1f}",
        ]
        if self.uploader:
            lines.append(f"👤 {self.uploader}")
        if self.tags:
            tags_display = ", ".join(self.tags[:10])
            if len(self.tags) > 10:
                tags_display += f" ... (+{len(self.tags) - 10})"
            lines.append(f"🏷️ {tags_display}")
        if self.language:
            lines.append(f"🌐 {self.language}")
        if self.posted:
            lines.append(f"📅 {self.posted}")
        return "\n".join(lines)


@dataclass
class ImagePage:
    index: int
    image_url: str
    filename: str = ""
    local_path: str = ""
    page_url: str = ""


@dataclass
class DownloadTask:
    gallery: Gallery
    total_pages: int
    completed_pages: int = 0
    failed_pages: int = 0
    status: str = "pending"
    temp_dir: str = ""
    archive_path: str = ""
