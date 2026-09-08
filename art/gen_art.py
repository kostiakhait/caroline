import requests, base64, os, io
import numpy as np
from PIL import Image

def generate_image(description, size='1024x1024'):
    r = requests.post('https://squirrelwisdom.com/', json={
        '.command': 'generateImage',
        'key': '01Az8nB8mB4cCV',
        'description': description,
        'size': size,
    }, timeout=120)
    data = r.json()
    if data.get('.status') != 'ok':
        raise Exception(f"API error: {data}")
    return data['result']

def resize_image(b64, width, height, mode='stretch'):
    r = requests.post('https://squirrelwisdom.com/', json={
        '.command': 'resizeImage',
        'key': '01Az8nB8mB4cCV',
        'data': b64,
        'mode': mode,
        'width': width,
        'height': height,
        'format': 'PNG',
    }, timeout=30)
    data = r.json()
    if data.get('.status') != 'ok':
        raise Exception(f"API error: {data}")
    return data['result']

def chroma_key_magenta(b64_data):
    img_bytes = base64.b64decode(b64_data)
    img = Image.open(io.BytesIO(img_bytes)).convert('RGBA')
    data = np.array(img, dtype=np.int32)
    R, G, B = data[:,:,0], data[:,:,1], data[:,:,2]
    mask = (R > 160) & (B > 160) & ((R - G) > 80) & ((B - G) > 80)
    data[mask, 3] = 0
    return Image.fromarray(data.astype(np.uint8))

ASSETS = os.path.dirname(__file__)

BG = "PURE FLAT MAGENTA background, hex #FF00FF, completely uniform, no gradients, no texture, no vignette."

SPLASH_PROMPT = (
    "A beautiful, elegant middle-aged professional woman secretary with a confident, "
    "independent expression, sitting at a sleek futuristic reception desk in a "
    "high-tech executive office -- like Tony Stark's personal secretary. Minimalist "
    "sci-fi architecture, holographic displays, clean chrome and glass surfaces, "
    "cinematic soft lighting. The entire scene is tightly contained within a perfect "
    "circle occupying the full frame -- everything outside that circle is pure flat "
    "magenta. " + BG + " Painterly digital illustration style, no text, no logos, no watermark."
)

ICON_PROMPT = (
    "A rich, detailed, FULL-COLOR painterly app icon illustration of an elegant "
    "middle-aged woman secretary sitting at a sleek futuristic desk, matching the "
    "style of a premium modern app icon (like a polished digital painting, full "
    "color depth and shading, not a flat vector, not a monochrome silhouette, no "
    "limited palette). Navy-blue business attire, warm natural skin tone, "
    "sophisticated lighting. Centered composition with generous padding so it "
    "reads well at both large and small sizes. " + BG
)

def make(name, prompt, size='1024x1024'):
    print(f"Generating {name}...")
    b64 = generate_image(prompt, size)
    img = chroma_key_magenta(b64)
    path = os.path.join(ASSETS, f'{name}_raw.png')
    img.save(path, 'PNG')
    transparent_px = int((np.array(img)[:, :, 3] == 0).sum())
    print(f"  -> {path}  ({transparent_px} transparent pixels)")
    return img

if __name__ == '__main__':
    make('splash', SPLASH_PROMPT, '1024x1024')
    make('icon', ICON_PROMPT, '1024x1024')
    print("Done.")
