from PIL import Image
import numpy as np

def make_transparent_png(img_in):
    img = Image.fromarray(img_in)
    img_rgba = img.convert("RGBA")
    pixel_data = img.load()

    for y in range(img_rgba.size[1]):
        for x in range(img_rgba.size[0]):
            if pixel_data[x, y][0] == 255 and pixel_data[x, y][1] == 255 and pixel_data[x, y][2] == 255:
                pixel_data[x, y] = (255, 255, 255, 0)

    output_array = np.array(img_rgba)
    return img_rgba


# Load the input image as a NumPy array
input_array = np.array(Image.open("input_image.png"))

# Remove the background and get the output array
output_array = make_transparent_png(input_array)

# Do something with the output array (e.g. save it to a file)