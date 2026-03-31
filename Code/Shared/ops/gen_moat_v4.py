# Copyright (c) 2026 Skip Snow. All rights reserved.
# Moat diagram v4 — Boss spec from SGML markup
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

fig, ax = plt.subplots(1, 1, figsize=(14, 12))
ax.set_xlim(0, 14)
ax.set_ylim(0, 12)
ax.axis('off')
fig.patch.set_facecolor('white')

ax.text(7, 11.5, 'Competitive Moat', ha='center', va='center',
        fontsize=22, fontweight='bold', color='#111827')

center = (7, 5.5)

# Rings — incremental "whiter" hue (same hue family, lighter each ring)
# Outermost is most saturated, innermost lightest (except center)
rings = [
    (4.5, '#fca5a5', '#dc2626', 'Regulatory Willingness'),     # red family
    (3.6, '#c4b5fd', '#7c3aed', 'Governance Framework'),       # purple family
    (2.7, '#6ee7b7', '#059669', 'Data + Network'),              # green family
    (1.9, '#93c5fd', '#2563eb', 'Consumer Trust'),              # blue family
    (1.2, '#fde68a', '#d97706', 'Patent'),                      # amber family
]

for radius, fc, ec, label in rings:
    ax.add_patch(mpatches.Circle(center, radius, facecolor=fc, edgecolor=ec,
                                  linewidth=2.5, alpha=0.6))

# Center — Naples yellow, Courier font, words fit inside
ax.add_patch(mpatches.Circle(center, 0.75, facecolor='#FADA5E', edgecolor='#b45309', linewidth=3))
ax.text(center[0], center[1] + 0.15, 'IP Protection', ha='center', va='center',
        fontsize=13, fontweight='bold', color='#dc2626', family='Courier New')
ax.text(center[0], center[1] - 0.15, '& Trade Secrets', ha='center', va='center',
        fontsize=11, fontweight='bold', color='#dc2626', family='Courier New')

# Labels in ovals circling the target with curved connector lines
label_data = [
    (4.5, '#dc2626', '#fca5a5', 'Regulatory\nWillingness', 150),   # angle degrees
    (3.6, '#7c3aed', '#c4b5fd', 'Governance\nFramework', 75),
    (2.7, '#059669', '#6ee7b7', 'Data +\nNetwork', 10),
    (1.9, '#2563eb', '#93c5fd', 'Consumer\nTrust', -50),
    (1.2, '#d97706', '#fde68a', 'Patent', -120),
]

for radius, ring_color, oval_color, label, angle_deg in label_data:
    angle = np.radians(angle_deg)

    # Oval position — outside the outermost ring
    oval_dist = 5.5
    ox = center[0] + oval_dist * np.cos(angle)
    oy = center[1] + oval_dist * np.sin(angle) * 0.7  # squish vertically

    # Oval
    oval_w, oval_h = 1.8, 0.9
    ax.add_patch(mpatches.FancyBboxPatch(
        (ox - oval_w/2, oy - oval_h/2), oval_w, oval_h,
        boxstyle='round,pad=0.15', facecolor=oval_color, edgecolor='#6b7280',
        linewidth=2, alpha=0.9, zorder=10))

    # Label text (same color as ring)
    ax.text(ox, oy, label, ha='center', va='center',
            fontsize=9, fontweight='bold', color=ring_color, zorder=11)

    # Curved connector line from oval to ring edge
    # Bezier approximation using arc
    ring_edge_x = center[0] + radius * np.cos(angle)
    ring_edge_y = center[1] + radius * np.sin(angle) * 0.7

    # Control point for curve — offset perpendicular
    mid_x = (ox + ring_edge_x) / 2 + 0.8 * np.cos(angle + np.pi/3)
    mid_y = (oy + ring_edge_y) / 2 + 0.8 * np.sin(angle + np.pi/3)

    # Draw curved line as segments through control point
    from matplotlib.path import Path
    import matplotlib.patches as mpl_patches

    verts = [(ox, oy), (mid_x, mid_y), (ring_edge_x, ring_edge_y)]
    codes = [Path.MOVETO, Path.CURVE3, Path.CURVE3]
    path = Path(verts, codes)
    patch = mpl_patches.PathPatch(path, facecolor='none', edgecolor=ring_color,
                                    linewidth=3, zorder=8)
    ax.add_patch(patch)

plt.tight_layout(pad=0.3)
fig.savefig('brain/BusinessArtifacts/diagrams/moat_diagram.png', dpi=150,
            bbox_inches='tight', facecolor='white', pad_inches=0.2)
plt.close()
print('Moat v4 done')
