"""
ytbatch

YouTube backlog pipeline: playlist -> IDs -> metadata + transcripts -> LLM
descriptions. Each stage is a module with a main(), wired to a console script
in pyproject.toml, and each is resumable on its own.

Nothing here imports the stage modules. batch_fetch pulls in yt-dlp and
youtube-transcript-api, gui pulls in tkinter, analyze pulls in a provider SDK -
importing the package should not drag in any of that.
"""
