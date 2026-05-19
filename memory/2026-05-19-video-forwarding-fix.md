# DEBUG REPORT - 2026-05-19

- Symptom: Twitter/X plugin image forwarding still works, but video forwarding fails after a recent AstrBot platform update. Runtime log shows `ActionFailed retcode=1200` from `NodeIKernelMsgService/sendMsg` while sending a `1920x1080` Twitter MP4 URL.
- Root cause: The failure happens at the AstrBot/NapCat send boundary, not in X media download. Runtime evidence later showed even a 480x270 / 1.1MB MP4 fails with the same `NodeIKernelMsgService/sendMsg` timeout, so bitrate/size alone is not the cause. The practical issue is that aiocqhttp/NapCat video message sending is unreliable in this environment after the platform update.
- Fix: Changed video sending in `main.py` to use `Video.fromFileSystem(str(vid_path))`. For aiocqhttp, prefer the smallest MP4 candidate first to avoid long high-resolution send waits. If the video message send fails with ordinary transport errors, fall back to OneBot `upload_group_file` / `upload_private_file`. If the QQ NT return contains `rich media transfer failed`, skip file upload and directly send the video URL fallback.
- Evidence: Added `tests/test_video_component_source.py`. It failed before the fix because filesystem video construction and file-upload fallback were absent, then passed after the fix.
- Regression test: `tests/test_video_component_source.py`
- Related: Image forwarding already used `Image.fromFileSystem(str(img_path))`, which explains why only videos were affected.
- Status: DONE_WITH_CONCERNS - verified by regression test and syntax checks; live AstrBot/NapCat send behavior still needs runtime confirmation with a real video tweet.
