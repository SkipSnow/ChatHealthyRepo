# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_biz_arch_diagrams.py — Overnight: rewrite biz arch + generate diagrams
# GPT decides diagram content, Claude renders, GPT reviews, Claude revises.

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
BRAIN_DIR = PROJECT_ROOT / "brain"
DIAGRAM_DIR = BRAIN_DIR / "BusinessArtifacts" / "diagrams"
DIAGRAM_DIR.mkdir(parents=True, exist_ok=True)

discuss_care = json.loads((BRAIN_DIR / "machine_artifacts" / "document_type_json" / "component_discuss_care.json").read_text(encoding="utf-8"))


def call_gpt(messages):
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-5.3-chat-latest",
        messages=messages,
    )
    return response.choices[0].message.content, response.usage.prompt_tokens, response.usage.completion_tokens


def generate_component_diagram(spec):
    """Three-component diagram with governance and infrastructure layers."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    fig, ax = plt.subplots(1, 1, figsize=(12, 7))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 7)
    ax.axis('off')
    fig.patch.set_facecolor('white')

    # Governance bar (top)
    ax.add_patch(mpatches.FancyBboxPatch((0.5, 5.8), 11, 0.8, boxstyle="round,pad=0.1", facecolor='#fef2f2', edgecolor='#dc2626', linewidth=2))
    ax.text(6, 6.2, 'Governance Layer', ha='center', va='center', fontsize=11, fontweight='bold', color='#dc2626')
    ax.text(6, 5.95, 'Safety | Consent | Policy | Floor Control', ha='center', va='center', fontsize=8, color='#991b1b')

    # Three components
    colors = [('#eff6ff', '#2563eb'), ('#ecfdf5', '#059669'), ('#f5f3ff', '#7c3aed')]
    names = ['Find Care', 'Evaluate Care', 'Discuss Care']
    taglines = ['Discover', 'Assess', 'Decide Together']
    prices = ['FREE forever', 'FREE now\nPaywall later', 'Premium tokens\n+ Vendor fees']
    x_positions = [0.7, 4.2, 7.7]

    for i, (x, name, tag, price, (fc, ec)) in enumerate(zip(x_positions, names, taglines, prices, colors)):
        ax.add_patch(mpatches.FancyBboxPatch((x, 2.2), 3.2, 3.2, boxstyle="round,pad=0.15", facecolor=fc, edgecolor=ec, linewidth=2))
        ax.text(x + 1.6, 4.8, name, ha='center', va='center', fontsize=13, fontweight='bold', color=ec)
        ax.text(x + 1.6, 4.3, tag, ha='center', va='center', fontsize=10, fontstyle='italic', color='#6b7280')
        ax.text(x + 1.6, 3.0, price, ha='center', va='center', fontsize=8, color='#374151')

    # Arrows between components
    ax.annotate('', xy=(4.2, 3.8), xytext=(3.9, 3.8), arrowprops=dict(arrowstyle='->', color='#374151', lw=2))
    ax.annotate('', xy=(7.7, 3.8), xytext=(7.4, 3.8), arrowprops=dict(arrowstyle='->', color='#374151', lw=2))

    # Infrastructure bar (bottom)
    ax.add_patch(mpatches.FancyBboxPatch((0.5, 0.5), 11, 1.2, boxstyle="round,pad=0.1", facecolor='#f9fafb', edgecolor='#6b7280', linewidth=2))
    ax.text(6, 1.3, 'Data + AI Infrastructure', ha='center', va='center', fontsize=11, fontweight='bold', color='#374151')
    ax.text(6, 0.9, 'NPI Registry | ClinicalTrials.gov | State Boards | LLMs | MongoDB', ha='center', va='center', fontsize=8, color='#6b7280')

    # Title
    ax.text(6, 6.85, 'ChatHealthy.ai — Component Architecture', ha='center', va='center', fontsize=14, fontweight='bold', color='#111827')

    plt.tight_layout()
    path = str(DIAGRAM_DIR / 'component_diagram.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    print(f"  Component diagram: {path}")
    return path


def generate_value_chain_diagram(spec):
    """Value chain flow: Find -> Evaluate -> Discuss with increasing value."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    fig, ax = plt.subplots(1, 1, figsize=(12, 5))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 5)
    ax.axis('off')
    fig.patch.set_facecolor('white')

    ax.text(6, 4.6, 'Care Decision Lifecycle', ha='center', va='center', fontsize=14, fontweight='bold', color='#111827')

    # Three stages as increasing-height bars
    heights = [2.0, 2.6, 3.2]
    colors = ['#2563eb', '#059669', '#7c3aed']
    labels = ['Find Care', 'Evaluate Care', 'Discuss Care']
    subtexts = ['Solo user\nAI assists\n\nFREE', 'Solo user\nAI assists\n\nTrust', 'Multi-user\nMulti-AI\n\nRevenue']
    x_positions = [1.0, 4.5, 8.0]

    for x, h, c, label, sub in zip(x_positions, heights, colors, labels, subtexts):
        y_bottom = 0.5
        ax.add_patch(mpatches.FancyBboxPatch((x, y_bottom), 2.5, h, boxstyle="round,pad=0.1", facecolor=c, edgecolor=c, linewidth=0, alpha=0.15))
        ax.add_patch(mpatches.FancyBboxPatch((x, y_bottom), 2.5, h, boxstyle="round,pad=0.1", facecolor='none', edgecolor=c, linewidth=2))
        ax.text(x + 1.25, y_bottom + h - 0.35, label, ha='center', va='center', fontsize=12, fontweight='bold', color=c)
        ax.text(x + 1.25, y_bottom + h/2 - 0.2, sub, ha='center', va='center', fontsize=9, color='#374151')

    # Arrows
    ax.annotate('', xy=(4.5, 2.0), xytext=(3.5, 2.0), arrowprops=dict(arrowstyle='->', color='#374151', lw=2.5))
    ax.annotate('', xy=(8.0, 2.5), xytext=(7.0, 2.5), arrowprops=dict(arrowstyle='->', color='#374151', lw=2.5))

    # Value label
    ax.annotate('', xy=(11.0, 0.5), xytext=(11.0, 3.7), arrowprops=dict(arrowstyle='<-', color='#9ca3af', lw=1.5))
    ax.text(11.3, 2.1, 'Value', ha='left', va='center', fontsize=9, color='#9ca3af', rotation=90)

    plt.tight_layout()
    path = str(DIAGRAM_DIR / 'value_chain.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    print(f"  Value chain: {path}")
    return path


def generate_revenue_flow_diagram(spec):
    """Revenue flow: Users and AI Vendors both pay the platform."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    fig, ax = plt.subplots(1, 1, figsize=(10, 6))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 6)
    ax.axis('off')
    fig.patch.set_facecolor('white')

    ax.text(5, 5.6, 'Revenue Model', ha='center', va='center', fontsize=14, fontweight='bold', color='#111827')

    # Platform center
    ax.add_patch(mpatches.FancyBboxPatch((3, 2.2), 4, 1.6, boxstyle="round,pad=0.2", facecolor='#f0fdf4', edgecolor='#059669', linewidth=3))
    ax.text(5, 3.3, 'ChatHealthy', ha='center', va='center', fontsize=14, fontweight='bold', color='#059669')
    ax.text(5, 2.8, 'The Healthcare Decision Room', ha='center', va='center', fontsize=9, color='#374151')

    # Users (left)
    ax.add_patch(mpatches.Circle((1.5, 3.0), 0.8, facecolor='#eff6ff', edgecolor='#2563eb', linewidth=2))
    ax.text(1.5, 3.0, 'Users', ha='center', va='center', fontsize=11, fontweight='bold', color='#2563eb')
    ax.annotate('', xy=(3.0, 3.0), xytext=(2.3, 3.0), arrowprops=dict(arrowstyle='->', color='#2563eb', lw=2))
    ax.text(2.65, 3.4, 'Premium\nTokens', ha='center', va='center', fontsize=8, color='#2563eb')

    # AI Vendors (right)
    ax.add_patch(mpatches.Circle((8.5, 3.0), 0.8, facecolor='#f5f3ff', edgecolor='#7c3aed', linewidth=2))
    ax.text(8.5, 3.0, 'AI\nVendors', ha='center', va='center', fontsize=10, fontweight='bold', color='#7c3aed')
    ax.annotate('', xy=(7.0, 3.0), xytext=(7.7, 3.0), arrowprops=dict(arrowstyle='->', color='#7c3aed', lw=2))
    ax.text(7.35, 3.4, 'Access\nFees', ha='center', va='center', fontsize=8, color='#7c3aed')

    # Free services (top)
    ax.add_patch(mpatches.FancyBboxPatch((2.5, 4.5), 5, 0.7, boxstyle="round,pad=0.1", facecolor='#fafafa', edgecolor='#d1d5db', linewidth=1))
    ax.text(5, 4.85, 'Find Care + Evaluate Care = FREE', ha='center', va='center', fontsize=10, fontweight='bold', color='#374151')

    # GOV-001 (bottom)
    ax.add_patch(mpatches.FancyBboxPatch((1.5, 0.5), 7, 0.7, boxstyle="round,pad=0.1", facecolor='#fef2f2', edgecolor='#dc2626', linewidth=1))
    ax.text(5, 0.85, 'GOV-001: Zero care-chain revenue. Technology partners only.', ha='center', va='center', fontsize=9, fontweight='bold', color='#dc2626')

    plt.tight_layout()
    path = str(DIAGRAM_DIR / 'revenue_flow.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    print(f"  Revenue flow: {path}")
    return path


def generate_discuss_care_room(spec):
    """Discuss Care session: humans, AIs, moderator, shared tools."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    fig, ax = plt.subplots(1, 1, figsize=(10, 8))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 8)
    ax.axis('off')
    fig.patch.set_facecolor('white')

    ax.text(5, 7.6, 'Discuss Care — The Room', ha='center', va='center', fontsize=14, fontweight='bold', color='#111827')

    # Session room
    ax.add_patch(mpatches.FancyBboxPatch((1.5, 1.5), 7, 5.5, boxstyle="round,pad=0.3", facecolor='#fafafa', edgecolor='#374151', linewidth=2))

    # Moderator (top center)
    ax.add_patch(mpatches.RegularPolygon((5, 6.3), 3, radius=0.4, facecolor='#fef3c7', edgecolor='#d97706', linewidth=2))
    ax.text(5, 6.3, 'MOD', ha='center', va='center', fontsize=7, fontweight='bold', color='#92400e')
    ax.text(5, 5.7, 'Bot Moderator', ha='center', va='center', fontsize=8, color='#92400e')

    # Humans (left side)
    human_positions = [(2.5, 5.0), (2.5, 3.8), (2.5, 2.6)]
    human_labels = ['Mom (Host)', 'Daughter', 'Sister']
    for (x, y), label in zip(human_positions, human_labels):
        ax.add_patch(mpatches.Circle((x, y), 0.45, facecolor='#eff6ff', edgecolor='#2563eb', linewidth=2))
        ax.text(x, y, '👤', ha='center', va='center', fontsize=14)
        ax.text(x + 0.7, y, label, ha='left', va='center', fontsize=9, color='#1e40af')

    # AI Models (right side)
    ai_positions = [(7.5, 5.0), (7.5, 3.8), (7.5, 2.6)]
    ai_labels = ['Claude', 'GPT', 'Gemini']
    ai_colors = ['#d4a373', '#74c69d', '#a8dadc']
    for (x, y), label, color in zip(ai_positions, ai_labels, ai_colors):
        ax.add_patch(mpatches.FancyBboxPatch((x - 0.4, y - 0.35), 0.8, 0.7, boxstyle="round,pad=0.1", facecolor=color, edgecolor='#374151', linewidth=1.5))
        ax.text(x, y, label, ha='center', va='center', fontsize=8, fontweight='bold', color='#1a1a1a')
        ax.text(x - 0.7, y, label, ha='right', va='center', fontsize=9, color='#374151')

    # Floor indicator
    ax.add_patch(mpatches.FancyBboxPatch((3.8, 4.3), 2.4, 0.6, boxstyle="round,pad=0.1", facecolor='#dcfce7', edgecolor='#16a34a', linewidth=2))
    ax.text(5, 4.6, 'FLOOR: Daughter', ha='center', va='center', fontsize=9, fontweight='bold', color='#16a34a')

    # Shared tools (bottom)
    ax.add_patch(mpatches.FancyBboxPatch((2.5, 1.7), 5, 0.6, boxstyle="round,pad=0.1", facecolor='#f9fafb', edgecolor='#6b7280', linewidth=1.5))
    ax.text(5, 2.0, 'Shared Tools: Providers | Trials | Specialties | Quality', ha='center', va='center', fontsize=8, fontweight='bold', color='#374151')

    # Queue indicator
    ax.text(5, 3.3, 'Queue: Mom > Claude > GPT', ha='center', va='center', fontsize=8, color='#6b7280', fontstyle='italic')

    plt.tight_layout()
    path = str(DIAGRAM_DIR / 'discuss_care_room.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    print(f"  Discuss Care room: {path}")
    return path


def generate_moat_diagram(spec):
    """Concentric moat diagram with readable labels."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    fig, ax = plt.subplots(1, 1, figsize=(14, 10))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 10)
    ax.axis('off')
    fig.patch.set_facecolor('white')

    ax.text(7, 9.5, 'Competitive Moat', ha='center', va='center', fontsize=20, fontweight='bold', color='#111827')

    center = (7, 4.8)

    # Concentric rings (outer to inner)
    rings = [
        (4.2, '#fef2f2', '#dc2626', 'Regulatory Willingness'),
        (3.3, '#f5f3ff', '#7c3aed', 'Governance Framework'),
        (2.4, '#ecfdf5', '#059669', 'Data + Network Effects'),
        (1.6, '#eff6ff', '#2563eb', 'Consumer Trust'),
        (0.9, '#fef3c7', '#d97706', 'Patent'),
    ]

    for radius, fc, ec, label in rings:
        ax.add_patch(mpatches.Circle(center, radius, facecolor=fc, edgecolor=ec, linewidth=2.5, alpha=0.6))

    # Labels positioned to the right with lines pointing to rings
    label_x = 12.0
    label_data = [
        (4.2, '#dc2626', 'Regulatory Willingness', 'AI vendors avoid FDA/HIPAA'),
        (3.3, '#7c3aed', 'Governance Framework', 'Safety, consent, floor control'),
        (2.4, '#059669', 'Data + Network', 'Provider signals compound'),
        (1.6, '#2563eb', 'Consumer Trust', 'Years to build, seconds to lose'),
        (0.9, '#d97706', 'Patent', 'LLM quality signal synthesis'),
    ]
    for i, (radius, color, label, sublabel) in enumerate(label_data):
        y = center[1] + radius * 0.7
        ax.text(label_x, y, label, ha='left', va='center', fontsize=12, fontweight='bold', color=color)
        ax.text(label_x, y - 0.35, sublabel, ha='left', va='center', fontsize=9, color='#6b7280')
        # Line from ring to label
        ring_edge_x = center[0] + radius * 0.7
        ax.plot([ring_edge_x, label_x - 0.1], [center[1] + radius * 0.5, y], color=color, linewidth=1, alpha=0.5)

    # Core
    ax.add_patch(mpatches.Circle(center, 0.6, facecolor='#1a1a1a', edgecolor='#1a1a1a', linewidth=0))
    ax.text(center[0], center[1] + 0.15, 'Trade Secrets', ha='center', va='center', fontsize=11, fontweight='bold', color='white')
    ax.text(center[0], center[1] - 0.15, '& IP Protection', ha='center', va='center', fontsize=11, fontweight='bold', color='white')

    plt.tight_layout()
    path = str(DIAGRAM_DIR / 'moat_diagram.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    print(f"  Moat diagram: {path}")
    return path


def main():
    print("=" * 60)
    print("Generating Business Architecture Diagrams")
    print("=" * 60)

    # Step 1: Ask GPT what diagrams and content
    print("\nStep 1: Asking GPT for diagram specifications...")
    messages = [
        {"role": "system", "content": json.dumps({
            "role": "Chief Business Architect",
            "identity": "GPT",
            "rules": [
                "Think deeply. Review twice.",
                "You are specifying diagrams for an investor-grade business architecture document.",
                "Diagrams must be simple: boxes, lines, white backgrounds, black text. No crossed lines.",
                "Each diagram must tell one clear story.",
                "The tone is inevitability — this platform HAS to exist.",
            ]
        })},
        {"role": "user", "content": f"""Review these 5 diagrams I'm about to generate for the ChatHealthy.ai investor document.
For each, tell me what to ADD, REMOVE, or CHANGE. Be specific.

1. COMPONENT DIAGRAM: Three boxes (Find Care, Evaluate Care, Discuss Care) with governance bar on top and infrastructure bar on bottom.
2. VALUE CHAIN: Three increasing-height bars showing Find->Evaluate->Discuss with value arrow.
3. REVENUE FLOW: Users and AI Vendors both pay the platform. Free services on top. GOV-001 on bottom.
4. DISCUSS CARE ROOM: Session with humans (Mom, Daughter, Sister), AI models (Claude, GPT, Gemini), moderator, floor indicator, shared tools, queue.
5. MOAT: Concentric rings — outer: Regulatory Willingness, then Governance, Data+Network, Trust, inner: Patent.

Context: {json.dumps(discuss_care.get('moat', {}), default=str)}
Revenue: {json.dumps(discuss_care.get('pricing_model', {}), default=str)}

Return JSON with feedback per diagram. Be brief."""},
    ]

    gpt_feedback, tin, tout = call_gpt(messages)
    print(f"  GPT feedback: {tin} in / {tout} out")

    # Step 2: Generate diagrams
    print("\nStep 2: Generating diagrams...")
    paths = []
    paths.append(generate_component_diagram(gpt_feedback))
    paths.append(generate_value_chain_diagram(gpt_feedback))
    paths.append(generate_revenue_flow_diagram(gpt_feedback))
    paths.append(generate_discuss_care_room(gpt_feedback))
    paths.append(generate_moat_diagram(gpt_feedback))

    # Step 3: Ask GPT to review
    print("\nStep 3: Asking GPT to review diagram descriptions...")
    messages.append({"role": "assistant", "content": gpt_feedback})
    messages.append({"role": "user", "content": """Diagrams generated. Five PNG files created:
1. component_diagram.png — 3 boxes, governance top, infrastructure bottom
2. value_chain.png — 3 increasing bars, value arrow
3. revenue_flow.png — Users + Vendors -> Platform, free services top, GOV-001 bottom
4. discuss_care_room.png — Session with 3 humans, 3 AIs, moderator, floor, queue, shared tools
5. moat_diagram.png — 5 concentric rings, patent at center

Are these sufficient for an investor document? Any critical missing diagram?
Return brief JSON."""})

    gpt_review, tin2, tout2 = call_gpt(messages)
    print(f"  GPT review: {tin2} in / {tout2} out")

    # Save review
    review_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "biz_arch_diagram_review.json"
    review_path.write_text(gpt_review if gpt_review.strip().startswith("{") else json.dumps({"raw": gpt_review}), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"Complete. 5 diagrams generated.")
    print(f"Tokens: {tin+tin2} in / {tout+tout2} out")
    print(f"Diagrams: {DIAGRAM_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
