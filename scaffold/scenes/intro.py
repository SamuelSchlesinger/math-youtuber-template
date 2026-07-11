from manim import *

from chalk_runtime import ChalkScene
from style import BG, WHITE


class Visual(ChalkScene):
    def construct(self):
        self.camera.background_color = BG
        title = Text(__TITLE_PYTHON__, font_size=72, color=WHITE)
        self.play_on("opening line", FadeIn(title), lead=0.2)
        self.finish()
