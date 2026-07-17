import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

def plot_expressions(expressions, x_min=0.1, x_max=10, output_name="plot.png"):
    x = np.linspace(x_min, x_max, 400)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for expr in expressions:
        y = eval(expr, {"x": x, "sin": np.sin, "cos": np.cos, "log": np.log, "exp": np.exp, "sqrt": np.sqrt, "np": np})
        ax.plot(x, y, label=expr)
    ax.legend()
    ax.set_xlabel("x")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_name, dpi=120)
    return {"output_file": output_name, "note": f"Plotted {len(expressions)} expression(s)"}