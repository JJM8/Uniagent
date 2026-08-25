#!/usr/bin/env python3

import matplotlib.pyplot as plt
import matplotlib.patches as patches

# Create figure and axis
fig, ax = plt.subplots(1, 1, figsize=(10, 6))

# Draw PCB base (dark green)
ax.add_patch(patches.Rectangle((0, 0), 10, 6, facecolor='#2E8B57', edgecolor='black', linewidth=1))

# Draw solder mask (orange) - slightly larger opening
ax.add_patch(patches.Rectangle((3, 2), 4, 2, facecolor='#FFA500', edgecolor='black', linewidth=1))

# Draw copper pad (gold/yellow) - actual connection point
ax.add_patch(patches.Rectangle((3.2, 2.2), 3.6, 1.6, facecolor='#FFD700', edgecolor='black', linewidth=0.5))

# Draw solder paste (light green) - where solder will be applied
ax.add_patch(patches.Rectangle((3.1, 2.1), 3.8, 1.8, facecolor='#90EE90', edgecolor='black', linewidth=0.5))

# Labels
ax.text(5, 0.5, 'PCB Base (FR4)', ha='center', va='center', fontsize=12, color='white')
ax.text(5, 1.5, 'Solder Mask (Orange)', ha='center', va='center', fontsize=12, color='black')
ax.text(5, 2.5, 'Copper Pad (Gold)', ha='center', va='center', fontsize=12, color='black')
ax.text(5, 3.5, 'Solder Paste (Light Green)', ha='center', va='center', fontsize=12, color='black')

# Title
ax.set_title('PCB Layers - What JLCPCB is Asking About', fontsize=14, weight='bold')

# Set limits and remove axes
ax.set_xlim(0, 10)
ax.set_ylim(0, 6)
ax.axis('off')

# Save the figure
plt.tight_layout()
plt.savefig('pcb_layers_visualization.png', dpi=150, bbox_inches='tight')
plt.close()
