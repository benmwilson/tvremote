"""Write app/assets/bg-full.png (the home screen's background), chat-bg-strip.png and chat-bg-bar.png: the background behind the stream while chat is on the TV (two soft colour glows over the
whole 1920x1080 screen, cut into the chat window's piece and the now-playing bar's piece, so the two join up). Baked into pictures because drawing
the glows as subtitle graphics re-rendered them every frame (about 15% of a core per window on the Pi). Needs Pillow; run on the PC after changing the look."""
import os

from PIL import Image, ImageFilter

W, H = 1920, 1080
BG = (0x0B, 0x0E, 0x14)
GLOWS = [(-200, -260, 1100, 760, (0x2E, 0x5A, 0x9A), 0.69, 140), (1000, 640, 1100, 760, (0x6A, 0x2E, 0x8A), 0.65, 140)]  # the same two glows as the multi-view screen behind several streams (app/shell.py wall_screen)


def render():
    img = Image.new("RGB", (W, H), BG)
    for x, y, w, h, colour, opacity, blur in GLOWS:
        layer = Image.new("L", (W + 800, H + 800), 0)  # room around the screen so a glow that spills over its edge is blurred, not cut
        layer.paste(int(255 * opacity), (x + 400, y + 400, x + 400 + w, y + 400 + h))
        mask = layer.filter(ImageFilter.GaussianBlur(blur)).crop((400, 400, 400 + W, 400 + H))
        img.paste(Image.new("RGB", (W, H), colour), (0, 0), mask)
    return img


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "assets")
    os.makedirs(out, exist_ok=True)
    full = render()
    full.save(os.path.join(out, "bg-full.png"), optimize=True)  # the home screen window plays this one, so everything shares the same pixels
    full.crop((1440, 0, 1920, 1080)).save(os.path.join(out, "chat-bg-strip.png"), optimize=True)
    full.crop((0, 810, 1440, 1080)).save(os.path.join(out, "chat-bg-bar.png"), optimize=True)
    print("written to", out)
