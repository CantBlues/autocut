import datetime
import logging
import os
import re

import srt
from moviepy import VideoFileClip, AudioFileClip, VideoClip, concatenate_videoclips, concatenate_audioclips
from moviepy.audio.fx import AudioNormalize

from . import utils


# Merge videos
class Merger:
    def __init__(self, args):
        self.args = args

    def write_md(self, videos):
        md = utils.MD(self.args.inputs[0], self.args.encoding)
        num_tasks = len(md.tasks())
        # Not overwrite if already marked as down or no new videos
        if md.done_editing() or num_tasks == len(videos) + 1:
            return

        md.clear()
        md.add_done_editing(False)
        md.add("\nSelect the files that will be used to generate `autocut_final.mp4`\n")
        base = lambda fn: os.path.basename(fn)
        for f in videos:
            md_fn = utils.change_ext(f, "md")
            video_md = utils.MD(md_fn, self.args.encoding)
            # select a few words to scribe the video
            desc = ""
            if len(video_md.tasks()) > 1:
                for _, t in video_md.tasks()[1:]:
                    m = re.findall(r"\] (.*)", t)
                    if m and "no speech" not in m[0].lower():
                        desc += m[0] + " "
                    if len(desc) > 50:
                        break
            md.add_task(
                False,
                f'[{base(f)}]({base(md_fn)}) {"[Edited]" if video_md.done_editing() else ""} {desc}',
            )
        md.write()

    def run(self):
        md_fn = self.args.inputs[0]
        md = utils.MD(md_fn, self.args.encoding)
        if not md.done_editing():
            return

        videos = []
        for m, t in md.tasks():
            if not m:
                continue
            m = re.findall(r"\[(.*)\]", t)
            if not m:
                continue
            fn = os.path.join(os.path.dirname(md_fn), m[0])
            logging.info(f"Loading {fn}")
            videos.append(VideoFileClip(fn))

        dur = sum([v.duration for v in videos])
        logging.info(f"Merging into a video with {dur / 60:.1f} min length")

        merged = concatenate_videoclips(videos)
        fn = os.path.splitext(md_fn)[0] + "_merged.mp4"
        merged.write_videofile(
            fn, audio_codec="aac", bitrate=self.args.bitrate
        )  # logger=None,
        logging.info(f"Saved merged video to {fn}")


# Cut media
class Cutter:
    def __init__(self, args):
        self.args = args

    def run(self):
        fns = {"srt": None, "media": None, "md": None}
        for fn in self.args.inputs:
            ext = os.path.splitext(fn)[1][1:]
            fns[ext if ext in fns else "media"] = fn

        assert fns["media"], "must provide a media filename"
        assert fns["srt"], "must provide a srt filename"

        is_video_file = utils.is_video(fns["media"].lower())
        outext = "mp4" if is_video_file else "mp3"
        output_fn = utils.change_ext(utils.add_cut(fns["media"]), outext)
        if utils.check_exists(output_fn, self.args.force):
            return

        with open(fns["srt"], encoding=self.args.encoding) as f:
            subs = list(srt.parse(f.read()))

        if fns["md"]:
            md = utils.MD(fns["md"], self.args.encoding)
            if not md.done_editing():
                return
            index = []
            for mark, sent in md.tasks():
                if not mark:
                    continue
                m = re.match(r"\\?\[(\d+)", sent.strip())
                if m:
                    index.append(int(m.groups()[0]))
            subs = [s for s in subs if s.index in index]
            logging.info(f'Cut {fns["media"]} based on {fns["srt"]} and {fns["md"]}')
        else:
            logging.info(f'Cut {fns["media"]} based on {fns["srt"]}')

        segments = []
        # Avoid disordered subtitles
        subs.sort(key=lambda x: x.start)
        for x in subs:
            if len(segments) == 0:
                segments.append(
                    {"start": x.start.total_seconds(), "end": x.end.total_seconds()}
                )
            else:
                if x.start.total_seconds() - segments[-1]["end"] < 0.5:
                    segments[-1]["end"] = x.end.total_seconds()
                else:
                    segments.append(
                        {"start": x.start.total_seconds(), "end": x.end.total_seconds()}
                    )

        if is_video_file:
            media = VideoFileClip(fns["media"])
        else:
            media = AudioFileClip(fns["media"])

        # Add a fade between two clips. Not quite necessary. keep code here for reference
        # fade = 0
        # segments = _expand_segments(segments, fade, 0, video.duration)
        # clips = [video.subclip(
        #         s['start'], s['end']).crossfadein(fade) for s in segments]
        # final_clip = editor.concatenate_videoclips(clips, padding = -fade)

        if not segments:
            logging.warning("No segments to cut. Please check your srt and md files.")
            media.close()
            return

        # Generate new subtitles with adjusted timestamps
        new_subs = self._generate_new_subtitles(subs, segments)
        srt_output_fn = utils.change_ext(utils.add_cut(fns["media"]), "srt")
        with open(srt_output_fn, "wb") as f:
            f.write(srt.compose(new_subs).encode(self.args.encoding, "replace"))
        logging.info(f"Saved new subtitles to {srt_output_fn}")

        clips = [media.subclipped(s["start"], s["end"]) for s in segments]
        if is_video_file:
            final_clip: VideoClip = concatenate_videoclips(clips)
            logging.info(
                f"Reduced duration from {media.duration:.1f} to {final_clip.duration:.1f}"
            )

            aud = final_clip.audio.with_fps(44100)
            final_clip = final_clip.without_audio().with_audio(aud)
            final_clip = final_clip.with_effects([AudioNormalize()])

            # an alternative to birate is use crf, e.g. ffmpeg_params=['-crf', '18']
            final_clip.write_videofile(
                output_fn, audio_codec="aac", bitrate=self.args.bitrate
            )
        else:
            from moviepy.audio.AudioClip import AudioClip
            final_clip: AudioClip = concatenate_audioclips(clips)
            logging.info(
                f"Reduced duration from {media.duration:.1f} to {final_clip.duration:.1f}"
            )

            final_clip = final_clip.with_effects([AudioNormalize()])
            final_clip.write_audiofile(
                output_fn, codec="libmp3lame", fps=44100, bitrate=self.args.bitrate
            )

        media.close()
        logging.info(f"Saved media to {output_fn}")

    def _generate_new_subtitles(self, subs, segments):
        """Generate new subtitles with adjusted timestamps based on selected segments"""
        new_subs = []
        current_time = 0.0
        sub_index = 1

        for seg in segments:
            seg_start = seg["start"]
            seg_end = seg["end"]
            seg_duration = seg_end - seg_start

            # Find subtitles that fall within this segment
            for sub in subs:
                sub_start = sub.start.total_seconds()
                sub_end = sub.end.total_seconds()

                # Check if subtitle overlaps with segment
                if sub_end <= seg_start or sub_start >= seg_end:
                    continue

                # Calculate new timestamps relative to the cut video
                new_start = max(0, sub_start - seg_start) + current_time
                new_end = min(sub_end - seg_start, seg_duration) + current_time

                if new_end > new_start:
                    new_subs.append(
                        srt.Subtitle(
                            index=sub_index,
                            start=datetime.timedelta(seconds=new_start),
                            end=datetime.timedelta(seconds=new_end),
                            content=sub.content,
                        )
                    )
                    sub_index += 1

            # Update current time for next segment
            current_time += seg_duration

        return new_subs
