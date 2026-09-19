"""One module per sidebar screen.

Each screen is two objects. The *model* is plain state -- what was loaded, what
is running, which plan is open -- and outlives the widgets: switching the
language rebuilds every widget from scratch, and a result that arrives while
that happens is applied to the model and drawn by whichever widgets exist when
it lands. The *screen* is the widgets and a `render()` that draws the model.
"""
