import requests, base64, os, io
from PIL import Image

def generate_image(description, size='1792x1024'):
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

OUT_DIR = os.path.join(os.path.dirname(__file__), 'banners')
os.makedirs(OUT_DIR, exist_ok=True)

# Consistent character description reused in every prompt so all 24 banners
# read as the same person -- the API has no image-reference/img2img input,
# only a text description, so this text block is the only consistency lever
# available (matches the woman generated for splash.png/icon_1024.png).
CAROLINE = (
    "Caroline: an elegant, confident middle-aged woman with a stylish "
    "grey-streaked brown bob haircut, warm brown eyes, a tailored navy-blue "
    "business blazer over a cream blouse, a pearl necklace and earrings, "
    "warm confident smile, independent and capable demeanor"
)

STYLE = "Painterly digital illustration, rich color, cinematic lighting, no text, no logos, no watermark."

SCENES = [
    ("post_office",      f"{CAROLINE}, mailing a stack of letters at a vintage post office counter, brass scale and rubber stamps. {STYLE}"),
    ("holo_desk",        f"{CAROLINE}, typing at a sleek futuristic holographic desk with floating translucent screens, minimalist sci-fi office. {STYLE}"),
    ("rotary_phone",     f"{CAROLINE}, answering a vintage rotary telephone in a cozy 1950s-style office, warm lamp light. {STYLE}"),
    ("archive",          f"{CAROLINE}, filing paper documents among towering shelves of an old archive library, dust motes in sunbeams. {STYLE}"),
    ("spaceship_cockpit",f"{CAROLINE}, piloting a small spacecraft from its cockpit, stars and a nebula through the viewport, glowing control panels. {STYLE}"),
    ("spaceship_repair", f"{CAROLINE}, repairing a spaceship engine with a wrench, sparks flying, wearing a jacket over her blazer, in a metallic engine bay. {STYLE}"),
    ("boardroom_coffee", f"{CAROLINE}, serving coffee in a sleek glass-walled corporate boardroom during a meeting, city skyline outside. {STYLE}"),
    ("server_room",      f"{CAROLINE}, standing among glowing server racks in a data center, blue light on her face, tablet in hand. {STYLE}"),
    ("ancient_library",  f"{CAROLINE}, reading an old scroll in a grand ancient library with tall stone arches and candlelight. {STYLE}"),
    ("drone_traffic",    f"{CAROLINE}, directing a swarm of delivery drones over a futuristic city skyline at dusk, holographic map in hand. {STYLE}"),
    ("piano_dictation",  f"{CAROLINE}, sitting at a grand piano taking dictation through a headset, elegant concert hall around her. {STYLE}"),
    ("skiing",           f"{CAROLINE}, skiing down a snowy mountain slope while holding a tablet, bright winter sunlight, sense of motion. {STYLE}"),
    ("sailboat",         f"{CAROLINE}, steering a sailboat on open water while on a video call, wind in her hair, ocean spray. {STYLE}"),
    ("robot_boardroom",  f"{CAROLINE}, presenting a chart on a whiteboard to a room full of attentive robots, bright modern office. {STYLE}"),
    ("warehouse",        f"{CAROLINE}, packing a parcel for shipping in a busy warehouse, shelves of boxes stretching into the distance. {STYLE}"),
    ("hot_air_balloon",  f"{CAROLINE}, organizing a calendar on a tablet while riding in a hot air balloon basket over green hills. {STYLE}"),
    ("red_carpet",       f"{CAROLINE}, greeting a guest on a red carpet under camera flashes, elegant evening lighting. {STYLE}"),
    ("car_mechanic",     f"{CAROLINE}, working under a car hood as a mechanic, wiping her oily hands with a rag, garage with tools. {STYLE}"),
    ("greenhouse",       f"{CAROLINE}, watering plants in a lush glass greenhouse while holding a walkie-talkie, soft green light. {STYLE}"),
    ("submarine",        f"{CAROLINE}, standing on a submarine bridge relaying messages, sonar screens glowing green around her. {STYLE}"),
    ("art_gallery",      f"{CAROLINE}, curating and cataloguing paintings in an elegant art gallery, soft museum lighting. {STYLE}"),
    ("catering_kitchen", f"{CAROLINE}, organizing catering trays in a busy professional kitchen, stainless steel counters, steam rising. {STYLE}"),
    ("movie_set",        f"{CAROLINE}, holding a clapperboard as assistant director on a film set, studio lights and cameras around her. {STYLE}"),
    ("zero_gravity",     f"{CAROLINE}, floating in zero gravity inside a space station fixing a glowing console, Earth visible through a window. {STYLE}"),
]

if __name__ == '__main__':
    for i, (name, prompt) in enumerate(SCENES, start=1):
        path = os.path.join(OUT_DIR, f'{i:02d}_{name}.png')
        if os.path.exists(path):
            print(f"[{i:02d}/24] {name}: already exists, skipping")
            continue
        print(f"[{i:02d}/24] Generating {name}...")
        b64 = generate_image(prompt, '1792x1024')
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGB')
        img.save(path, 'PNG')
        print(f"  -> {path}  ({img.size[0]}x{img.size[1]})")
    print("Done.")
