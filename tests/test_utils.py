import numpy as np

from .context import sdf

def test_utils_plot_join_line():
    t = {'id':np.array(['a','b','c','b']),
         'x' :np.array([1  ,2  ,3  ,4  ]),
         'y' :np.array([5  ,6  ,7  ,8  ])}
    x,y = sdf.utils.plot_join_line(t,'id','x','y')
    assert(np.all(np.equal(x,[2,4])))
    assert(np.all(np.equal(y,[6,8])))
