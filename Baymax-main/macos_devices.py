"""macOS camera metadata only; importing/enumerating does not start capture."""


def _framework():
    try:
        import AVFoundation
        return AVFoundation
    except ImportError as exc:
        raise RuntimeError("Mac camera discovery needs pyobjc-framework-AVFoundation. "
                           "Install requirements-pc.txt with this environment's Python.") from exc


def camera_devices():
    # Match the pinned OpenCV 4.13 AVFoundation backend exactly: video + muxed,
    # sorted by NSString uniqueID. Do not deduplicate: that would change indices.
    from functools import cmp_to_key
    av = _framework()
    import objc
    with objc.autorelease_pool():
        devices = list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeVideo))
        devices += list(av.AVCaptureDevice.devicesWithMediaType_(av.AVMediaTypeMuxed))
        devices.sort(key=cmp_to_key(lambda a, b: int(a.uniqueID().compare_(b.uniqueID()))))
        return [{"index": index, "name": str(device.localizedName()), "uid": str(device.uniqueID())}
                for index, device in enumerate(devices)]


def resolve_camera(index, uid="", name=""):
    devices = camera_devices()
    if uid:
        matches = [device for device in devices if device["uid"] == uid]
    elif name:
        # Old settings without a UID can migrate only if the name is unique.
        matches = [device for device in devices if device["name"] == name]
    else:
        matches = [device for device in devices if device["index"] == index]
    if len(matches) != 1:
        raise RuntimeError("Saved Mac camera is unavailable or ambiguous. Refresh devUI, choose a camera and Save.")
    return matches[0]["index"]


def check_camera_permission():
    av = _framework()
    status = av.AVCaptureDevice.authorizationStatusForMediaType_(av.AVMediaTypeVideo)
    if status in (av.AVAuthorizationStatusDenied, av.AVAuthorizationStatusRestricted):
        raise RuntimeError("macOS camera permission is denied or restricted. Open System Settings > Privacy & Security > "
                           "Camera and allow the app launching Ember (for example Terminal), then restart it. "
                           "Managed Macs may require an administrator.")
    # Undetermined: let OpenCV request permission in the main-thread test process.
    # The user may need to approve and click Test camera again.
