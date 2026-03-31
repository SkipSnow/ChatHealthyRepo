# Copyright (c) 2026 Skip Snow. All rights reserved.
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

fig, ax = plt.subplots(1, 1, figsize=(14, 8))
ax.set_xlim(0, 14)
ax.set_ylim(0, 8)
ax.axis('off')
fig.patch.set_facecolor('white')

ax.text(7, 7.7, 'Evaluate Care', ha='center', va='center', fontsize=16, fontweight='bold', color='#111827')
ax.text(7, 7.3, 'You choose what matters. The algorithm listens.', ha='center', va='center', fontsize=10, fontstyle='italic', color='#6b7280')

# -- RED STEEL 3D CAGE (fish tank, no glass) --
cx, cy = 0.3, 0.7
cw, ch = 6.0, 6.0
dx, dy = 0.5, 0.4

# Back face
for x1, y1, x2, y2 in [(cx+dx, cy+dy, cx+cw+dx, cy+dy), (cx+cw+dx, cy+dy, cx+cw+dx, cy+ch+dy),
                         (cx+cw+dx, cy+ch+dy, cx+dx, cy+ch+dy), (cx+dx, cy+ch+dy, cx+dx, cy+dy)]:
    ax.plot([x1, x2], [y1, y2], color='#7f1d1d', linewidth=3, zorder=0)

# Depth connectors
for fx, fy, bx, by in [(cx, cy, cx+dx, cy+dy), (cx+cw, cy, cx+cw+dx, cy+dy),
                         (cx, cy+ch, cx+dx, cy+ch+dy), (cx+cw, cy+ch, cx+cw+dx, cy+ch+dy)]:
    ax.plot([fx, bx], [fy, by], color='#dc2626', linewidth=3, zorder=0)

# Front face
for x1, y1, x2, y2 in [(cx, cy, cx+cw, cy), (cx+cw, cy, cx+cw, cy+ch),
                         (cx+cw, cy+ch, cx, cy+ch), (cx, cy+ch, cx, cy)]:
    ax.plot([x1, x2], [y1, y2], color='#dc2626', linewidth=4, zorder=6)

# Corner rivets
for rx, ry in [(cx, cy), (cx+cw, cy), (cx, cy+ch), (cx+cw, cy+ch)]:
    ax.add_patch(mpatches.Circle((rx, ry), 0.08, facecolor='#991b1b', edgecolor='#7f1d1d', linewidth=2, zorder=7))

# -- GRAPH NETWORK inside cage (63 nodes, chaotic) --
np.random.seed(99)
n_nodes = 63
node_x = cx + 0.3 + np.random.uniform(0, cw - 0.6, n_nodes)
node_y = cy + 0.3 + np.random.uniform(0, ch - 0.6, n_nodes)
for i in range(0, n_nodes, 3):
    node_x[i] += np.random.normal(0, 0.4)
    node_y[i] += np.random.normal(0, 0.3)
node_x = np.clip(node_x, cx + 0.2, cx + cw - 0.2)
node_y = np.clip(node_y, cy + 0.2, cy + ch - 0.2)

palette = ['#3b82f6','#2563eb','#1d4ed8','#10b981','#059669','#047857',
           '#ef4444','#dc2626','#b91c1c','#8b5cf6','#7c3aed','#6d28d9',
           '#f59e0b','#d97706','#b45309','#ec4899','#db2777','#06b6d4',
           '#0891b2','#84cc16','#65a30d']
node_colors = [palette[np.random.randint(0, len(palette))] for _ in range(n_nodes)]

for i in range(n_nodes):
    for j in range(i+1, n_nodes):
        dist = np.sqrt((node_x[i]-node_x[j])**2 + (node_y[i]-node_y[j])**2)
        if dist < 1.8 and np.random.random() < 0.28:
            ax.plot([node_x[i], node_x[j]], [node_y[i], node_y[j]],
                    color=node_colors[i], linewidth=0.5, alpha=0.45, zorder=1)

for i in range(n_nodes):
    c = node_colors[i]
    shape = np.random.randint(0, 6)
    x, y = node_x[i], node_y[i]
    size = np.random.uniform(0.07, 0.16)
    z = np.random.randint(2, 5)
    if shape == 0:
        ax.add_patch(mpatches.Circle((x, y), size, facecolor=c, edgecolor='white', linewidth=0.5, alpha=0.85, zorder=z))
    elif shape == 1:
        ax.add_patch(mpatches.RegularPolygon((x, y), 4, radius=size, facecolor=c, edgecolor='white', linewidth=0.5, alpha=0.85, zorder=z))
    elif shape == 2:
        ax.add_patch(mpatches.RegularPolygon((x, y), 3, radius=size, facecolor=c, edgecolor='white', linewidth=0.5, alpha=0.85, zorder=z))
    elif shape == 3:
        ax.add_patch(mpatches.RegularPolygon((x, y), 5, radius=size, facecolor=c, edgecolor='white', linewidth=0.5, alpha=0.85, zorder=z))
    elif shape == 4:
        ax.add_patch(mpatches.RegularPolygon((x, y), 6, radius=size, facecolor=c, edgecolor='white', linewidth=0.5, alpha=0.85, zorder=z))
    else:
        ax.add_patch(mpatches.FancyBboxPatch((x-size*0.7, y-size*0.7), size*1.4, size*1.4,
                     boxstyle='round,pad=0.02', facecolor=c, edgecolor='white', linewidth=0.5, alpha=0.85, zorder=z))

ax.text(3.3, 0.35, 'Every signal connects to every other signal. LLMs see what humans cannot.',
        ha='center', va='center', fontsize=7, fontstyle='italic', color='#9ca3af')

# -- YELLOW CAT5 plugged into tank and mixer --
# Left plug (into tank wall)
ax.add_patch(mpatches.FancyBboxPatch((6.2, 3.55), 0.45, 0.6, boxstyle='round,pad=0.03',
             facecolor='#fbbf24', edgecolor='#92400e', linewidth=2, zorder=8))
ax.add_patch(mpatches.Rectangle((6.3, 3.6), 0.25, 0.12, facecolor='#fef3c7', edgecolor='#92400e', linewidth=1, zorder=9))

# Cable with droop
cable_x = np.linspace(6.65, 8.35, 60)
cable_y = 3.85 + 0.2 * np.sin(np.linspace(0, np.pi, 60)) - 0.1
ax.plot(cable_x, cable_y, color='#fbbf24', linewidth=5, solid_capstyle='round', zorder=8)
ax.plot(cable_x, cable_y, color='#f59e0b', linewidth=2, solid_capstyle='round', zorder=8)

# Right plug (into mixer)
ax.add_patch(mpatches.FancyBboxPatch((8.35, 3.55), 0.45, 0.6, boxstyle='round,pad=0.03',
             facecolor='#fbbf24', edgecolor='#92400e', linewidth=2, zorder=10))
ax.add_patch(mpatches.Rectangle((8.45, 3.6), 0.25, 0.12, facecolor='#fef3c7', edgecolor='#92400e', linewidth=1, zorder=11))

# -- MIXER --
mx, my = 8.8, 1.0
mw, mh = 4.7, 5.8
ax.add_patch(mpatches.FancyBboxPatch((mx, my), mw, mh, boxstyle='round,pad=0.15',
             facecolor='#111827', edgecolor='#374151', linewidth=2, zorder=10))

levers = [
    ('Prescribing habits', 0.7, '#3b82f6'),
    ('What do they treat', 0.8, '#10b981'),
    ('Where did they go to school', 0.5, '#ef4444'),
    ('Have they been sued', 0.3, '#8b5cf6'),
    ('Bedside manner', 0.6, '#f59e0b'),
]
for i, (label, level, color) in enumerate(levers):
    y = my + mh - 0.7 - i * 1.05
    ax.plot([mx+0.3, mx+mw-0.3], [y, y], color='#4b5563', linewidth=2.5, zorder=11)
    fill_x = mx + 0.3 + level * (mw - 0.6)
    ax.plot([mx+0.3, fill_x], [y, y], color=color, linewidth=4, zorder=11)
    ax.add_patch(mpatches.Circle((fill_x, y), 0.12, facecolor='white', edgecolor=color, linewidth=2, zorder=12))
    ax.text(mx+0.3, y+0.25, label, ha='left', va='center', fontsize=7, color='#d1d5db', zorder=12)

ax.add_patch(mpatches.Circle((11.1, 0.4), 0.35, facecolor='#eff6ff', edgecolor='#2563eb', linewidth=2, zorder=12))
ax.text(11.1, 0.4, 'YOU', ha='center', va='center', fontsize=8, fontweight='bold', color='#2563eb', zorder=13)

ax.add_patch(mpatches.FancyBboxPatch((9.8, 0.0), 1.8, 0.4, boxstyle='round,pad=0.05',
             facecolor='#fef3c7', edgecolor='#d97706', linewidth=1, zorder=12))
ax.text(10.7, 0.2, 'PATENT PENDING', ha='center', va='center', fontsize=6, fontweight='bold', color='#92400e', zorder=13)

plt.tight_layout(pad=0.3)
fig.savefig('brain/BusinessArtifacts/diagrams/evaluate_care_diagram.png', dpi=150, bbox_inches='tight', facecolor='white', pad_inches=0.2)
plt.close()
print('Red steel cage + yellow Cat5 + mixer')
