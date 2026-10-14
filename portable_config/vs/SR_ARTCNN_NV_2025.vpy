# Legacy entry point. Keep the shared preset and its options in SR_ARTCNN_NV_2K.vpy.
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name('SR_ARTCNN_NV_2K.vpy')),
               init_globals={'video_in': video_in})
