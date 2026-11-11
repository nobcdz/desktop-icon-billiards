"""Rounded, DPI-aware frost surfaces for the Tk control panel."""
import tkinter as tk
from tkinter import font

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageTk


def frost_image(width, height, radius, top, bottom, rim):
    top_rgb, bottom_rgb = ImageColor.getrgb(top), ImageColor.getrgb(bottom)
    gradient = Image.new('RGB', (1, height))
    gradient.putdata([tuple(round(a + (b - a) * y / max(1, height - 1)) for a, b in zip(top_rgb, bottom_rgb))
                      for y in range(height)])
    surface = gradient.resize((width, height)).convert('RGBA')
    grain = Image.effect_noise((width, height), 4).convert('RGB')
    grain = ImageChops.add(surface.convert('RGB'), grain, offset=-128)
    surface = Image.blend(surface, grain.convert('RGBA'), .32)
    mask = Image.new('L', (width * 2, height * 2))
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, width * 2 - 1, height * 2 - 1), radius=radius * 2, fill=255)
    surface.putalpha(mask.resize((width, height), Image.Resampling.LANCZOS))
    border = Image.new('RGBA', mask.size)
    ImageDraw.Draw(border).rounded_rectangle((0, 0, width * 2 - 1, height * 2 - 1),
                                             radius=radius * 2, outline=rim, width=2)
    surface.alpha_composite(border.resize((width, height), Image.Resampling.LANCZOS))
    return surface


def frost_style(style, master, name, kind, palette, variants=()):
    """Use a nine-slice texture while retaining ttk text, focus and bindings."""
    scale = master.winfo_fpixels('1i') / 96
    side, radius = round(32 * scale), round(9 * scale)

    def texture(colors):
        texture = Image.new('RGBA', (side, side), master.cget('bg'))
        texture.alpha_composite(frost_image(side, side, radius, *colors))
        return ImageTk.PhotoImage(texture, master=master)

    images = [texture(palette), *(texture(colors) for _, colors in variants)]
    master.frost_images.extend(images)
    element = name.removesuffix(f'.T{kind}') + f'.{kind}.frost'
    style.element_create(element, 'image', images[0],
                         *((state, image) for (state, _), image in zip(variants, images[1:])),
                         border=(radius + 1, side // 2 - 1, radius + 1, side // 2 - 1),
                         sticky='nsew')
    style.layout(name, [(element, {'sticky': 'nsew', 'children': [
        (f'{kind}.padding', {'sticky': 'nsew', 'children': [
            (f'{kind}.label', {'sticky': 'nsew'}),
        ]}),
    ]})])


class StatCard(tk.Canvas):
    """Show a live caption and value directly over the frosted panel."""

    def __init__(self, parent, caption, value, background, palette, muted, ink):
        self.dpi_scale = parent.winfo_fpixels('1i') / 96
        self.value_width = None
        self.value_text = None
        self.caption, self.value = caption, value
        self.palette = palette
        self.caption_font = font.Font(parent, family='Microsoft YaHei UI', size=9)
        self.value_font = font.Font(parent, family='Segoe UI', size=24, weight='bold')
        super().__init__(parent, bg=background, bd=0, highlightthickness=0,
                         width=round(120 * self.dpi_scale), height=round(84 * self.dpi_scale))
        self.surface_id = self.create_image(0, 0, anchor='nw')
        self.caption_id = self.create_text(0, 0, anchor='nw', fill=muted, font=self.caption_font)
        self.value_id = self.create_text(0, 0, anchor='sw', fill=ink, font=self.value_font)
        self.traces = [(var, var.trace_add('write', self.refresh)) for var in (caption, value)]
        self.bind('<Configure>', self.resize_card)
        self.bind('<Destroy>', self.remove_traces)
        self.refresh()

    def refresh(self, *_):
        self.itemconfigure(self.caption_id, text=self.caption.get())
        value = self.value.get()
        if self.value_text == value:
            return
        if self.value_width is not None:
            size = self.value_font.cget('size')
            measured = max(1, self.value_font.measure(value))
            target = min(24, max(10, int(size * self.value_width / measured)))
            if target != size:
                self.value_font.configure(size=target)
        self.itemconfigure(self.value_id, text=value)
        self.value_text = value

    def resize_card(self, event):
        if event.width < 2 or event.height < 2:
            return
        pad = round(13 * self.dpi_scale)
        self.surface = ImageTk.PhotoImage(
            frost_image(event.width, event.height, round(12 * self.dpi_scale), *self.palette), master=self)
        self.itemconfigure(self.surface_id, image=self.surface)
        self.coords(self.caption_id, pad, pad)
        self.coords(self.value_id, pad, event.height - round(9 * self.dpi_scale))
        self.value_width = event.width - 2 * pad
        self.value_text = None
        self.refresh()

    def remove_traces(self, _event):
        for var, token in self.traces:
            var.trace_remove('write', token)
