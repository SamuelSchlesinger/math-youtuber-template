from manim import *

from chalk_runtime import ChalkScene
from style import BG, WHITE


class Visual(ChalkScene):
    def construct(self):
        self.camera.background_color = BG
        closing = Text("Closing thought", font_size=64, color=WHITE)
        self.play_on("closing line", FadeIn(closing), lead=0.2)
        self.finish()

