"""Drawing helpers for the table, the balls, the pockets and the cue.

Every function returns Pillow ``RGBA`` images that are blitted as per-pixel
alpha layered windows by the shell.
"""
import math

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


BALL_COLORS = (
    (229, 170, 20), (31, 86, 185), (200, 43, 48), (107, 57, 153),
    (224, 108, 25), (30, 133, 85), (140, 45, 54), (35, 39, 46),
)


def numbered_ball_art(icon, number):
    """Render a shaded pool ball with the original icon and its rack number."""
    side = 96
    center = (side - 1) / 2
    palette_index = (number - 1) % 15
    color = BALL_COLORS[palette_index % len(BALL_COLORS)]
    ivory = (246, 243, 232)
    pixels = []
    for py in range(side):
        y = (py - center) / center
        for px in range(side):
            x = (px - center) / center
            z2 = 1 - x * x - y * y
            if z2 <= 0:
                pixels.append((0, 0, 0, 0))
                continue
            z = math.sqrt(z2)
            light = max(0, -.46 * x - .49 * y + .74 * z)
            shine = math.exp(-((x + .34) ** 2 + (y + .40) ** 2) / .038)
            base = ivory if palette_index >= 8 and abs(y) > .52 else color
            rgb = tuple(min(255, round(channel * (.46 + .54 * light) + 130 * shine))
                        for channel in base)
            alpha = min(255, round((1 - math.hypot(x, y)) * center * 255))
            pixels.append((*rgb, alpha))

    ball = Image.new('RGBA', (side, side))
    ball.putdata(pixels)
    outline = ball.getchannel('A')
    emblem = ImageOps.contain(icon, (74, 74), Image.Resampling.LANCZOS)
    ball.alpha_composite(emblem, ((side - emblem.width) // 2, 46 - emblem.height // 2))
    draw = ImageDraw.Draw(ball)
    label = str(number)
    font = ImageFont.load_default(size=19)
    text_width = draw.textlength(label, font=font)
    if text_width > 24:
        font = ImageFont.load_default(size=max(1, int(19 * 24 / text_width)))
    draw.ellipse((55, 53, 86, 84), fill=ivory, outline=(124, 123, 117), width=1)
    draw.text((70.5, 68.5), label, font=font, anchor='mm', fill=(23, 26, 30))
    ball.putalpha(outline)
    return ball


def table_art(table, wallpaper=None):
    """Draw the table frame and pockets; returns ``(frame, origin_x, origin_y)``."""
    left, top, right, bottom = table.bounds
    width = right - left
    height = bottom - top
    pad = math.ceil(table.pocket_radius) + 5
    origin_x, origin_y = left - pad, top - pad
    minimal = table.desktop and table.manual
    if table.desktop:
        origin_x, origin_y, outer_right, outer_bottom = table.work_area
        frame = Image.new('RGBA', (outer_right - origin_x, outer_bottom - origin_y),
                          (8, 26, 25, 40 if minimal else 57))
    else:
        frame = Image.new('RGBA', (width + 2 * pad + 1, height + 2 * pad + 1))
    draw = ImageDraw.Draw(frame)
    rail = max(9, min(22, pad // 2))
    x0 = left - origin_x
    y0 = top - origin_y
    if not minimal:
        draw.rounded_rectangle((x0 - rail, y0 - rail, x0 + width + rail, y0 + height + rail), radius=rail * 2,
                               fill=(75, 51, 38, 178 if table.desktop else 238),
                               outline=(202, 149, 91, 200 if table.desktop else 244), width=3)
    felt = (0, 0, 0, 0) if table.desktop else (14, 82, 63, 156)
    if wallpaper is not None:
        blur = wallpaper.filter(ImageFilter.GaussianBlur(radius=max(10, height / 55)))
        frame.paste(blur.crop((x0, y0, x0 + width + 1, y0 + height + 1)), (x0, y0))
        felt = None
    draw.rectangle((x0, y0, x0 + width, y0 + height), fill=felt,
                   outline=(105, 221, 179, 184) if minimal else (61, 155, 120, 168 if table.desktop else 238),
                   width=3 if minimal else 5)
    r = round(table.pocket_radius)
    for px, py in table.pockets:
        x = px - origin_x
        y = py - origin_y
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(11, 22, 21, 215 if minimal else 248),
                     outline=(105, 221, 179, 135) if minimal else (153, 116, 77, 255), width=2 if minimal else 3)
        inset = r * .23
        draw.ellipse((x - r + inset, y - r + inset, x + r - inset, y + r - inset), fill=(4, 10, 11, 255))
    return frame, origin_x, origin_y


def charge_art(percent, font):
    """Draw the charge label and its filled progress bar."""
    pad = max(4, font.size // 2)
    width = font.size * 8
    height = font.size * 2 + pad
    frame = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(frame)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=pad,
                           fill=(13, 25, 23, 248), outline=(105, 221, 179, 240))
    draw.text((pad, height // 2), '力度', font=font, anchor='lm', fill=(235, 250, 243))

    left = pad * 2 + font.size * 2
    top = (height - pad) // 2
    bar_width = width - pad - left
    draw.rounded_rectangle((left, top, left + bar_width - 1, top + pad - 1),
                           radius=pad // 2, fill=(49, 76, 70))
    fill_width = round(bar_width * percent / 100)
    if fill_width:
        draw.rounded_rectangle((left, top, left + fill_width - 1, top + pad - 1),
                               radius=min(pad, fill_width) // 2, fill=(111, 239, 185))
    return frame


def guide_art(lines):
    """Draw the opening path and the two possible outgoing paths."""
    colors = {'incoming': (235, 250, 245, 196), 'white': (167, 216, 255, 208), 'target': (112, 239, 181, 224)}
    points = [point for _, start, end in lines for point in (start, end)]
    xs, ys = zip(*points)
    left, top = min(xs) - 5, min(ys) - 5
    width = math.ceil((max(xs) - left + 6) / 64) * 64
    height = math.ceil((max(ys) - top + 6) / 64) * 64
    frame = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(frame)
    for role, start, end in lines:
        x, y = start[0] - left, start[1] - top
        end_x, end_y = end[0] - left, end[1] - top
        length = math.dist(start, end)
        dx, dy = (end_x - x) / length, (end_y - y) / length
        color = colors[role]
        for offset in range(0, max(1, math.ceil(length - 5)), 16):
            stop = min(max(0., length - 5), offset + 8)
            draw.line((x + dx * offset, y + dy * offset, x + dx * stop, y + dy * stop),
                      fill=color, width=2)
        draw.ellipse((end_x - 4, end_y - 4, end_x + 4, end_y + 4), outline=color, width=2)
    return frame, left, top


def white_art(radius, shaded=False):
    """Draw the cue ball, optionally shaded as a lit sphere."""
    side = max(3, math.ceil(radius * 2) + 4)
    frame = Image.new('RGBA', (side * 3, side * 3))
    if shaded:
        center = (side * 3 - 1) / 2
        r = max(1, radius * 3)
        pixels = []
        for py in range(side * 3):
            y = (py - center) / r
            for px in range(side * 3):
                x = (px - center) / r
                z2 = 1 - x * x - y * y
                if z2 < 0:
                    pixels.append((0, 0, 0, 0))
                    continue
                z = math.sqrt(z2)
                light = max(0, -.46 * x - .49 * y + .74 * z)
                shine = math.exp(-((x + .34) ** 2 + (y + .40) ** 2) / .038)
                value = min(255, round(157 + 96 * light + 32 * shine))
                pixels.append((value, min(255, value + 4), min(255, value + 9), 255))
        frame.putdata(pixels)
        return frame.resize((side, side), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(frame)
    draw.ellipse((5, 5, side * 3 - 6, side * 3 - 6), fill=(246, 249, 250, 255), outline=(134, 155, 169, 255), width=3)
    draw.ellipse((side * .65, side * .55, side * 1.15, side * 1.05), fill=(255, 255, 255, 255))
    return frame.resize((side, side), Image.Resampling.LANCZOS)


def pocket_particles(frame, index, fade):
    """Draw the ring of sparks that follows a sinking icon."""
    frame = frame.copy()
    draw = ImageDraw.Draw(frame)
    center = frame.width / 2
    for i in range(9):
        angle = i * math.tau / 9 + index * .61
        distance = fade * (20 + i % 3 * 8)
        x = center + math.cos(angle) * distance
        y = center + math.sin(angle) * distance
        size = max(.7, (1 - fade) * (2.5 + i % 3))
        alpha = round(230 * math.sin(math.pi * fade) ** .65)
        draw.ellipse((x - size, y - size, x + size, y + size), fill=(173, 255, 221, alpha))
    return frame


def cue_art(cue, thickness=1):
    """Draw a cue from ``(tip, tail)``; returns ``(frame, offset_x, offset_y)``."""
    tip, tail = cue
    side = math.ceil(math.dist(tip, tail)) + 18
    x = round((tip[0] + tail[0] - side) / 2)
    y = round((tip[1] + tail[1] - side) / 2)
    start = (tail[0] - x, tail[1] - y)
    end = (tip[0] - x, tip[1] - y)
    grip = (start[0] + (end[0] - start[0]) * .37, start[1] + (end[1] - start[1]) * .37)
    frame = Image.new('RGBA', (side, side))
    draw = ImageDraw.Draw(frame)
    draw.line((start, end), fill=(60, 37, 22, 220), width=round(7 * thickness))
    draw.line((start, end), fill=(239, 193, 126, 255), width=round(4 * thickness))
    draw.line((start, grip), fill=(127, 79, 48, 255), width=round(5 * thickness))
    r = 2 * thickness
    draw.ellipse((end[0] - r, end[1] - r, end[0] + r, end[1] + r), fill=(126, 207, 228, 255))
    return frame, x, y
