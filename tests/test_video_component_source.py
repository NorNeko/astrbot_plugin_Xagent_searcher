from pathlib import Path


def test_video_send_uses_filesystem_factory():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(
        encoding="utf-8"
    )

    assert "Video.fromFileSystem(str(vid_path))" in source
    assert "Video(str(vid_path))" not in source


def test_video_send_uploads_file_after_generic_video_component_failure():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(
        encoding="utf-8"
    )

    assert "_get_video_variant_candidates" in source
    assert "candidates = video_info[\"candidates\"]" in source
    assert "for candidate in candidates" in source
    assert "_upload_video_file_fallback" in source
    assert "upload_group_file" in source
    assert "upload_private_file" in source
    assert "视频消息发送失败，尝试作为文件上传" in source
    assert "视频发送失败，已降级为直链" in source


def test_rich_media_transfer_failure_skips_file_upload():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(
        encoding="utf-8"
    )

    assert "_is_rich_media_transfer_failed" in source
    assert "rich media transfer failed" in source
    assert "视频命中 QQ 富媒体风控，直接降级为直链" in source
    assert "not self._is_rich_media_transfer_failed(e)" in source
