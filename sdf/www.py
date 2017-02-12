import io
import os

import numpy as np

from jinja2 import Template
from bokeh.resources import CDN

from . import plotting
from . import db
from . import templates
from .utils import SdfError
from . import config as cfg


def www_all(results,update=False):
    """Generate www material."""

    print(" Web")

    # see whether index.html needs updating (unless update enforced)
    index_file = results[0].path+'/index.html'
    
    if os.path.exists(index_file):
        pkltime = []
        for r in results:
            pkltime.append( os.path.getmtime(r.pickle) )

        if os.path.getmtime(index_file) > np.max(pkltime):
            if not update:
                print("   no update needed")
                return
        print("   updating")
    else:
        print("   generating")

    index(results,file=index_file)


def index(results,file='index.html'):
    """Make index.html - landing page with an SED and other info."""

    script,div = plotting.sed_components(results)

    template = Template(templates.index)
    bokeh_js = CDN.render_js()
    bokeh_css = CDN.render_css()
    html = template.render(bokeh_js=bokeh_js,
                           bokeh_css=bokeh_css,
                           css=templates.css,
                           plot_script=script,
                           plot_div=div,
                           phot_file=os.path.basename(results[0].rawphot),
                           main_id=results[0].obs_keywords['main_id'],
                           spty=results[0].obs_keywords['sp_type'],
                           ra=results[0].obs_keywords['raj2000'],
                           dec=results[0].obs_keywords['dej2000'],
                           plx=results[0].obs_keywords['plx_value'],
                           models=results[0].main_results_text()
                           )

    with io.open(file, mode='w', encoding='utf-8') as f:
        f.write(html)

