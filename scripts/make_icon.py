from PIL import Image, ImageDraw

S = 256
BEIGE = (242, 234, 217, 255)
INK = (43, 38, 32, 255)
RED = (217, 96, 74, 255)
ORANGE = (232, 162, 104, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

d.rounded_rectangle([8, 8, 248, 248], radius=48, fill=BEIGE)
d.ellipse([28, 28, 132, 132], fill=RED)
d.rectangle([132, 44, 228, 140], fill=ORANGE)
d.polygon([(128, 150), (36, 224), (220, 224)], fill=INK)

img.save("static/icon.png")
img.save(
    "static/icon.ico",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("icons saved")
