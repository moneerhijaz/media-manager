"""Validate both the OpenCV package layout and bundled detector data."""
from importlib.metadata import distributions
from pathlib import Path


def check():
    variants = {"opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless"}
    installed = {d.metadata["Name"].lower() for d in distributions()} & variants
    if len(installed) != 1:
        return False, "Expected one OpenCV distribution; found: " + ", ".join(sorted(installed))
    try:
        import cv2
        path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if not path.is_file():
            return False, "Bundled face-detector XML is missing"
        if cv2.CascadeClassifier(str(path)).empty():
            return False, "Bundled face detector could not load"
    except Exception as exc:
        return False, f"OpenCV validation failed: {type(exc).__name__}"
    return True, "One OpenCV distribution; bundled face detector loads"


if __name__ == "__main__":
    ok, message = check()
    print(message)
    raise SystemExit(0 if ok else 1)
