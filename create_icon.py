from PIL import Image, ImageDraw, ImageFont
import os

def create_icon(output_dir="."):
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    
    for size in sizes:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Background - railway blue
        padding = size // 8
        draw.rounded_rectangle(
            [padding, padding, size - padding, size - padding],
            radius=size // 6,
            fill=(30, 80, 160)
        )
        
        # Inner highlight
        inner = padding + size // 16
        draw.rounded_rectangle(
            [inner, inner, size - inner, size - inner],
            radius=size // 8,
            fill=(40, 100, 180)
        )
        
        # Letter "T" for TCR
        font_size = int(size * 0.55)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except:
            font = ImageFont.load_default()
        
        bbox = draw.textbbox((0, 0), "T", font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (size - text_width) // 2
        y = (size - text_height) // 2 - bbox[1]
        
        # Shadow
        draw.text((x + 1, y + 1), "T", fill=(0, 0, 0, 100), font=font)
        # Text
        draw.text((x, y), "T", fill=(255, 255, 255), font=font)
        
        images.append(img)
    
    # Save ICO with multiple sizes
    ico_path = os.path.join(output_dir, "icon.ico")
    images[-1].save(ico_path, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])
    
    # Save PNG for Linux
    png_path = os.path.join(output_dir, "icon.png")
    images[-1].save(png_path, format="PNG")
    
    print(f"Created {ico_path} and {png_path}")
    return ico_path, png_path

if __name__ == "__main__":
    create_icon()
