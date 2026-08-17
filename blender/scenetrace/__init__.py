from .ui import CLASSES, register_properties, unregister_properties
from .graph_overlay import remove_graph_handler


def register():
    import bpy
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    register_properties()


def unregister():
    import bpy
    remove_graph_handler()
    unregister_properties()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
