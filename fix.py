with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

in_loop = False
for i, line in enumerate(lines):
    if "for rec_idx, row in pending:" in line:
        in_loop = True
        continue
    if in_loop:
        if 'console.print(Rule(style="cyan"))' in line:
            in_loop = False
        elif line.strip() != '':
            if len(line) - len(line.lstrip()) == 12:
                lines[i] = "    " + line
            elif len(line) - len(line.lstrip()) == 16:
                if not line.strip().startswith("except Exception") and not line.strip().startswith("try:") and not line.strip().startswith("if "):
                    # wait, this is getting complex.
                    pass

with open('main.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
