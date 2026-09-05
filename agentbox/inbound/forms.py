"""Read raw MIME form fields without decoding/re-encoding the email's bytes."""

from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.formparsers import MultiPartParser


class MimeFormParser(MultiPartParser):
    def __init__(self, *args, mime_field: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.mime_field = mime_field
        self.mime_bytes: bytes | None = None

    def on_part_end(self) -> None:
        if self._current_part.field_name == self.mime_field and self._current_part.file is None:
            self.mime_bytes = bytes(self._current_part.data)
        super().on_part_end()


@asynccontextmanager
async def mime_form(headers: Headers, body: bytes, mime_field: str, limit: int):
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        # latin-1 maps each byte one-to-one. Decode the provider metadata as UTF-8,
        # but leave the embedded MIME in its original charset/transfer encoding.
        fields = parse_qs(body.decode("latin-1"), encoding="latin-1", keep_blank_values=True, max_num_fields=30)
        values = fields.pop(mime_field, [""])
        if len(values) != 1:
            raise ValueError("Multiple raw MIME fields")
        raw = values[0].encode("latin-1")
        form = {key: values[-1].encode("latin-1").decode("utf-8") for key, values in fields.items()}
        yield form, raw
        return

    async def stream():
        yield body

    parser = MimeFormParser(headers, stream(), mime_field=mime_field, max_files=2, max_fields=30, max_part_size=limit)
    form = await parser.parse()
    try:
        if len(form.getlist(mime_field)) > 1:
            raise ValueError("Multiple raw MIME fields")
        yield form, parser.mime_bytes if parser.mime_bytes is not None else form.get(mime_field)
    finally:
        await form.close()
