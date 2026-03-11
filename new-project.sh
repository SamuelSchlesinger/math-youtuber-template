#!/bin/bash
# Scaffold a new math-youtuber project directory.
#
# Usage:
#   ./new-project.sh <project-dir> <video-title>
#
# Example:
#   ./new-project.sh ../03-11-2026 "lambda calculus"

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ $# -lt 2 ]; then
    echo "Usage: $0 <project-dir> <video-title>"
    echo "  e.g. $0 ../03-11-2026 \"lambda calculus\""
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
TITLE="$2"

if [ -d "$PROJECT_DIR" ] && [ "$(ls -A "$PROJECT_DIR" 2>/dev/null)" ]; then
    echo "ERROR: $PROJECT_DIR already exists and is not empty"
    exit 1
fi

echo "scaffolding project: $PROJECT_DIR"
echo "  title: $TITLE"

mkdir -p "$PROJECT_DIR"/{clips,output/segments}

# ── venv ──────────────────────────────────────────────────────
# look for a sibling .venv to symlink, otherwise create one
SIBLING_VENV=""
for d in "$PROJECT_DIR"/../*/; do
    if [ -d "$d/.venv" ] && [ "$d" != "$PROJECT_DIR/" ]; then
        SIBLING_VENV="$(cd "$d/.venv" && pwd)"
        break
    fi
done

if [ -n "$SIBLING_VENV" ]; then
    echo "  linking .venv -> $SIBLING_VENV"
    ln -sf "$SIBLING_VENV" "$PROJECT_DIR/.venv"
else
    echo "  creating .venv..."
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install -q manim f5-tts-mlx soundfile
fi

# ── find existing voice reference ─────────────────────────────
VOICE_REF=""
for d in "$PROJECT_DIR"/../*/clips/; do
    if [ -f "${d}my_voice_ref_24k.wav" ]; then
        VOICE_REF="$(cd "$d" && pwd)/my_voice_ref_24k.wav"
        break
    fi
done

# ── script.md ─────────────────────────────────────────────────
cat > "$PROJECT_DIR/script.md" << EOF
# $TITLE

## intro

> **[DIRECTOR: clear, unhurried. state the question directly.]**

(opening line goes here)

> **[CUT TO MANIM: S01_Intro scene]**
> title card with "$TITLE"

## closing

> **[DIRECTOR: measured pace. let the idea settle.]**

(closing line goes here)

> **[CUT TO MANIM: closing scene]**
> fade to black
EOF

# ── generate_narration.py ─────────────────────────────────────
REF_PATH="${VOICE_REF:-$PROJECT_DIR/clips/my_voice_ref_24k.wav}"
cat > "$PROJECT_DIR/generate_narration.py" << PYEOF
"""Generate all narration segments with explicit durations."""

from f5_tts_mlx.generate import generate
import subprocess
import os

BASE = "$PROJECT_DIR"
CLIPS = f"{BASE}/clips"

REF_AUDIO = "$REF_PATH"
REF_TEXT = (
    "replace this with the exact transcription of your reference audio"
)
REF_DUR = 12.25  # duration of reference audio in seconds

STEPS = 32   # 8=draft, 32=production
CFG = 3.5    # voice adherence (3.5 is the sweet spot)

# ── segments ──────────────────────────────────────────────────
# (filename, spoken text, duration in seconds)
# rule of thumb: ~2.5 words/sec at natural pace
SEGMENTS = [
    ("01_intro", "opening line goes here", 6.0),
    # add segments here...
    # ("02_concept", "explanation text", 12.0),
]


def generate_segment(name, text, speech_duration):
    out_path = f"{CLIPS}/{name}.wav"
    if os.path.exists(out_path):
        print(f"  skipping {name} (exists)")
        return out_path

    total_duration = REF_DUR + speech_duration
    print(f"  generating {name} ({speech_duration}s speech, {total_duration}s total)...")
    generate(
        generation_text=text,
        ref_audio_path=REF_AUDIO,
        ref_audio_text=REF_TEXT,
        duration=total_duration,
        speed=1.0,
        steps=STEPS,
        cfg_strength=CFG,
        output_path=out_path,
    )
    print(f"  done: {name}")
    return out_path


def concatenate_audio(segment_files, output_path):
    silence = f"{CLIPS}/_silence.wav"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        "anullsrc=r=24000:cl=mono", "-t", "0.5",
        "-c:a", "pcm_s16le", silence,
    ], capture_output=True)

    list_path = f"{CLIPS}/_concat_list.txt"
    with open(list_path, "w") as f:
        for i, seg in enumerate(segment_files):
            f.write(f"file '{seg}'\n")
            if i < len(segment_files) - 1:
                f.write(f"file '{silence}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path, "-c:a", "pcm_s16le", output_path,
    ], capture_output=True)
    print(f"full narration: {output_path}")


def main():
    os.makedirs(CLIPS, exist_ok=True)

    print("generating narration segments...")
    segment_files = []
    for name, text, dur in SEGMENTS:
        path = generate_segment(name, text, dur)
        segment_files.append(path)

    full_narration = f"{CLIPS}/full_narration.wav"
    print("\nconcatenating...")
    concatenate_audio(segment_files, full_narration)

    # verify durations
    print("\nsegment durations:")
    import soundfile as sf
    total = 0
    for name, _, _ in SEGMENTS:
        path = f"{CLIPS}/{name}.wav"
        data, sr = sf.read(path)
        dur = len(data) / sr
        total += dur
        print(f"  {name}: {dur:.1f}s")
    print(f"  TOTAL: {total:.1f}s")


if __name__ == "__main__":
    main()
PYEOF

# ── timed_scenes.py ───────────────────────────────────────────
cat > "$PROJECT_DIR/timed_scenes.py" << 'PYEOF'
"""Manim scenes — one class per narration segment.

Timing is aligned to narration at ~2.5 words/sec. Use the class
docstring to map spoken phrases to animation cues, and self.wait()
calls to hold until the next phrase lands.
"""

from manim import *

# ── colour palette ────────────────────────────────────────────
BG = "#1a1a2e"
GREEN = "#4ade80"
RED = "#f87171"
BLUE = "#60a5fa"
YELLOW = "#facc15"
WHITE = "#e2e8f0"

# ── durations (seconds) ──────────────────────────────────────
# keep in sync with generate_narration.py SEGMENTS and voiceover.sh
DUR = {
    "intro": 6.0,
    # "concept": 12.0,
}


class S01_Intro(Scene):
    """opening line goes here. | second phrase. | third phrase."""

    def setup(self):
        self.camera.background_color = BG

    def construct(self):
        d = DUR["intro"]
        elapsed = 0

        # "opening line goes here" — title appears with the words
        title = Text("Title", font_size=72, color=WHITE)
        self.play(FadeIn(title), run_time=0.8)
        elapsed += 0.8

        # "second phrase" — hold, then animate when phrase lands ~2.5s
        self.wait(1.7)
        elapsed += 1.7
        # ... next animation here ...

        self.wait(max(d - elapsed, 0.1))
PYEOF

# ── voiceover.sh ──────────────────────────────────────────────
SLUG=$(echo "$TITLE" | tr '[:upper:]' '[:lower:]' | tr ' ' '_' | tr -cd 'a-z0-9_')
cat > "$PROJECT_DIR/voiceover.sh" << SHEOF
#!/bin/bash
# Record voiceover, composite segments, check durations, preview.
#
# Usage:
#   ./voiceover.sh record          — record all segments
#   ./voiceover.sh record 05       — record just segment 05
#   ./voiceover.sh composite       — composite (prefers voiceover > TTS)
#   ./voiceover.sh durations       — compare voiceover vs video durations
#   ./voiceover.sh play 05         — preview segment 05 video

BASE="$PROJECT_DIR"
CLIPS="\$BASE/clips"
OUT="\$BASE/output"

# ── auto-detect render quality ───────────────────────────────
# check from highest to lowest quality
VIDEO=""
for q in 2160p60 1080p60 720p30 480p15; do
    if [ -d "\$BASE/media/videos/timed_scenes/\$q" ]; then
        VIDEO="\$BASE/media/videos/timed_scenes/\$q"
        break
    fi
done
if [ -z "\$VIDEO" ]; then
    echo "WARNING: no rendered videos found in media/videos/timed_scenes/"
    echo "  render first: manim render -qh timed_scenes.py SceneName"
    VIDEO="\$BASE/media/videos/timed_scenes/1080p60"  # fallback
fi
SLUG="$SLUG"

mkdir -p "\$OUT/segments"

# ── segment registry ──────────────────────────────────────────
# id:SceneClass:audio_stem:description
SEGMENTS=(
    "01:S01_Intro:01_intro:intro"
    # add segments here...
    # "02:S02_Concept:02_concept:the core concept"
)

# ── script text for recording prompts ─────────────────────────
# index matches SEGMENTS order
SCRIPTS=(
    "opening line goes here"
    # add matching script text...
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# recording
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

record_segment() {
    local num="\$1" scene="\$2" audio="\$3" desc="\$4" idx="\$5"
    local video_file="\$VIDEO/\${scene}.mp4"
    local vo_file="\$CLIPS/vo_\${audio}.wav"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Segment \$num: \$desc"

    local vdur
    vdur=\$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "\$video_file" 2>/dev/null)
    [ -n "\$vdur" ] && echo "  Video duration: \${vdur}s"
    echo "  Output: \$vo_file"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if [ -f "\$vo_file" ]; then
        local existing_dur
        existing_dur=\$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "\$vo_file" 2>/dev/null)
        echo "  (existing recording: \${existing_dur}s)"
    fi

    echo ""
    echo "  Script:"
    echo "  ───────"
    echo "  \${SCRIPTS[\$idx]}" | fmt -w 70 | sed 's/^/  /'
    echo ""
    echo "  (speak at your natural pace — animation will be adjusted to fit)"
    echo ""

    while true; do
        read -rp "  (r)ecord / (s)kip / (p)layback / (q)uit? " choice
        case "\$choice" in
            r)
                echo ""
                echo "  3..."
                sleep 1
                echo "  2..."
                sleep 1
                echo "  1..."
                sleep 1
                echo "  Recording... press ENTER when done"
                echo ""

                rec -q -r 24000 -c 1 -b 16 "\$vo_file" 2>/dev/null &
                REC_PID=\$!

                if [ -f "\$video_file" ]; then
                    ffplay -autoexit -window_title "Segment \$num" \
                        -x 960 -y 540 \
                        "\$video_file" 2>/dev/null &
                    FFPLAY_PID=\$!
                else
                    FFPLAY_PID=""
                fi

                read -rp "  (press ENTER to stop recording) "

                kill \$REC_PID 2>/dev/null
                wait \$REC_PID 2>/dev/null
                [ -n "\$FFPLAY_PID" ] && kill \$FFPLAY_PID 2>/dev/null && wait \$FFPLAY_PID 2>/dev/null

                local rec_dur
                rec_dur=\$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "\$vo_file" 2>/dev/null)
                echo ""
                echo "  Saved: \$vo_file (\${rec_dur}s)"

                read -rp "  (k)eep / (r)e-record / (p)lay back? " choice2
                case "\$choice2" in
                    k) return ;;
                    p)
                        ffplay -autoexit -nodisp "\$vo_file" 2>/dev/null
                        read -rp "  (k)eep / (r)e-record? " choice3
                        [ "\$choice3" = "r" ] && continue
                        return
                        ;;
                    r) continue ;;
                    *) return ;;
                esac
                ;;
            s) return ;;
            p)
                if [ -f "\$vo_file" ]; then
                    ffplay -autoexit -nodisp "\$vo_file" 2>/dev/null
                fi
                ;;
            q) exit 0 ;;
            *) ;;
        esac
    done
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# durations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

do_durations() {
    echo ""
    printf "%-4s  %-30s  %8s  %8s  %8s\n" "SEG" "DESCRIPTION" "VIDEO" "VOICE" "DIFF"
    printf "%-4s  %-30s  %8s  %8s  %8s\n" "───" "──────────────────────────────" "────────" "────────" "────────"
    for entry in "\${SEGMENTS[@]}"; do
        IFS=: read -r num scene audio desc <<< "\$entry"
        local video_file="\$VIDEO/\${scene}.mp4"
        local vo_file="\$CLIPS/vo_\${audio}.wav"

        vdur=\$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "\$video_file" 2>/dev/null)

        if [ -f "\$vo_file" ]; then
            adur=\$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "\$vo_file" 2>/dev/null)
            diff=\$(python3 -c "d=float('\${adur}')-float('\${vdur}'); print(f'{d:+.1f}s' + (' !!!' if abs(d)>2 else ''))")
            printf "%-4s  %-30s  %7.1fs  %7.1fs  %s\n" "\$num" "\${desc:0:30}" "\$vdur" "\$adur" "\$diff"
        else
            printf "%-4s  %-30s  %7.1fs  %8s  %s\n" "\$num" "\${desc:0:30}" "\$vdur" "—" "(no recording)"
        fi
    done
    echo ""
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# composite
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

do_composite() {
    echo "=== compositing segments ==="

    for entry in "\${SEGMENTS[@]}"; do
        IFS=: read -r num scene audio desc <<< "\$entry"
        local video_file="\$VIDEO/\${scene}.mp4"
        local vo_file="\$CLIPS/vo_\${audio}.wav"
        local tts_file="\$CLIPS/\${audio}.wav"
        local output_file="\$OUT/segments/seg_\${num}.mp4"

        # prefer voiceover, fall back to TTS
        if [ -f "\$vo_file" ]; then
            audio_file="\$vo_file"
            src="voice"
        elif [ -f "\$tts_file" ]; then
            audio_file="\$tts_file"
            src="TTS"
        else
            echo "WARNING: no audio for segment \$num"
            continue
        fi

        if [ ! -f "\$video_file" ]; then
            echo "WARNING: missing video \$video_file"
            continue
        fi

        echo "  \$num (\$scene + \$src)..."

        # if audio is longer than video, freeze last frame
        adur=\$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "\$audio_file" 2>/dev/null)
        vdur=\$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "\$video_file" 2>/dev/null)
        longer=\$(python3 -c "print('audio' if float('\${adur}') > float('\${vdur}') + 0.5 else 'ok')")

        if [ "\$longer" = "audio" ]; then
            pad=\$(python3 -c "print(f'{float(\"\${adur}\") - float(\"\${vdur}\") + 0.5:.1f}')")
            ffmpeg -y -i "\$video_file" -i "\$audio_file" \
                -filter_complex "[0:v]tpad=stop_mode=clone:stop_duration=\${pad}[vout]" \
                -map "[vout]" -map 1:a \
                -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p \
                -af "loudnorm=I=-16:TP=-1.5:LRA=11" \
                -c:a aac -b:a 192k \
                -shortest -movflags +faststart \
                "\$output_file" 2>/dev/null
        else
            ffmpeg -y -i "\$video_file" -i "\$audio_file" \
                -c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p \
                -af "loudnorm=I=-16:TP=-1.5:LRA=11" \
                -c:a aac -b:a 192k \
                -shortest -movflags +faststart \
                "\$output_file" 2>/dev/null
        fi
    done

    echo ""
    echo "=== concatenating final video ==="

    CONCAT_LIST="\$OUT/concat_list.txt"
    > "\$CONCAT_LIST"
    for entry in "\${SEGMENTS[@]}"; do
        IFS=: read -r num scene audio desc <<< "\$entry"
        echo "file 'segments/seg_\${num}.mp4'" >> "\$CONCAT_LIST"
    done

    # pass 1: measure loudness
    echo "  pass 1: measuring loudness..."
    LOUDNORM_STATS=\$(ffmpeg -y -f concat -safe 0 \
        -i "\$CONCAT_LIST" \
        -af loudnorm=I=-14:TP=-1:LRA=11:print_format=json \
        -f null - 2>&1 | grep -A 20 '"input_')

    INPUT_I=\$(echo "\$LOUDNORM_STATS" | grep input_i | sed 's/[^0-9.-]//g')
    INPUT_TP=\$(echo "\$LOUDNORM_STATS" | grep input_tp | sed 's/[^0-9.-]//g')
    INPUT_LRA=\$(echo "\$LOUDNORM_STATS" | grep input_lra | sed 's/[^0-9.-]//g')
    INPUT_THRESH=\$(echo "\$LOUDNORM_STATS" | grep input_thresh | sed 's/[^0-9.-]//g')

    echo "  measured: I=\${INPUT_I} LUFS, TP=\${INPUT_TP} dBTP, LRA=\${INPUT_LRA}"

    # pass 2: encode with measured values
    echo "  pass 2: encoding with loudnorm..."
    ffmpeg -y -f concat -safe 0 \
        -i "\$CONCAT_LIST" \
        -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
        -af loudnorm=I=-14:TP=-1:LRA=11:measured_I=\${INPUT_I}:measured_TP=\${INPUT_TP}:measured_LRA=\${INPUT_LRA}:measured_thresh=\${INPUT_THRESH}:linear=true \
        -c:a aac -b:a 192k \
        -movflags +faststart \
        "\$OUT/\$SLUG.mp4" 2>/dev/null

    echo ""
    echo "=== done ==="
    echo "final video: \$OUT/\$SLUG.mp4"
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

case "\${1:-}" in
    record)
        if [ -n "\${2:-}" ]; then
            idx=0
            for entry in "\${SEGMENTS[@]}"; do
                IFS=: read -r num scene audio desc <<< "\$entry"
                if [ "\$num" = "\$2" ]; then
                    record_segment "\$num" "\$scene" "\$audio" "\$desc" "\$idx"
                    exit 0
                fi
                idx=\$((idx + 1))
            done
            echo "Unknown segment: \$2"
            exit 1
        else
            echo "=== voiceover recording session ==="
            echo "Each segment: see script -> video autoplays -> speak along."
            echo "Press ENTER to stop recording. 's' to skip."
            echo ""
            idx=0
            for entry in "\${SEGMENTS[@]}"; do
                IFS=: read -r num scene audio desc <<< "\$entry"
                record_segment "\$num" "\$scene" "\$audio" "\$desc" "\$idx"
                idx=\$((idx + 1))
            done
            echo ""
            echo "=== all segments recorded ==="
            do_durations
        fi
        ;;
    composite)
        do_composite
        ;;
    durations)
        do_durations
        ;;
    play)
        if [ -z "\${2:-}" ]; then
            echo "Usage: \$0 play <segment_number>"
            exit 1
        fi
        for entry in "\${SEGMENTS[@]}"; do
            IFS=: read -r num scene audio desc <<< "\$entry"
            if [ "\$num" = "\$2" ]; then
                echo "Playing \$scene..."
                ffplay -autoexit -window_title "Segment \$num" \
                    -x 960 -y 540 \
                    "\$VIDEO/\${scene}.mp4" 2>/dev/null
                exit 0
            fi
        done
        echo "Unknown segment: \$2"
        ;;
    *)
        echo "Usage:"
        echo "  \$0 record          — record all segments (video autoplays)"
        echo "  \$0 record 05       — record just segment 05"
        echo "  \$0 durations       — compare voiceover vs video durations"
        echo "  \$0 composite       — composite (prefers voiceover > TTS)"
        echo "  \$0 play 05         — preview segment 05 video"
        echo ""
        echo "Segments:"
        for entry in "\${SEGMENTS[@]}"; do
            IFS=: read -r num scene audio desc <<< "\$entry"
            vo_file="\$CLIPS/vo_\${audio}.wav"
            if [ -f "\$vo_file" ]; then
                echo "  \$num: \$desc  [recorded]"
            else
                echo "  \$num: \$desc"
            fi
        done
        echo ""
        echo "Workflow:"
        echo "  1. ./voiceover.sh record       — record at your natural pace"
        echo "  2. ./voiceover.sh durations    — see what needs longer animations"
        echo "  3. ask claude to adjust DUR + re-render"
        echo "  4. ./voiceover.sh composite    — build final video"
        ;;
esac
SHEOF

chmod +x "$PROJECT_DIR/voiceover.sh"

# ── done ──────────────────────────────────────────────────────
echo ""
echo "=== project scaffolded ==="
echo ""
echo "  $PROJECT_DIR/"
echo "  ├── script.md              <- write your script here"
echo "  ├── generate_narration.py  <- add segments + voice ref"
echo "  ├── timed_scenes.py        <- one scene class per segment"
echo "  ├── voiceover.sh           <- record, composite, check durations"
echo "  ├── clips/"
echo "  ├── output/"
echo "  └── .venv/"
echo ""

if [ -n "$VOICE_REF" ]; then
    echo "  voice reference found: $VOICE_REF"
    echo "  (already set in generate_narration.py)"
else
    echo "  next step: record a voice reference"
    echo "    ../math-youtuber-skill/record-reference.sh $PROJECT_DIR/clips"
fi

echo ""
echo "  then: edit script.md, fill in generate_narration.py, and go!"
