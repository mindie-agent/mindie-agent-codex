"""Read persisted format declarations before retiring or installing a runtime."""
import json
from pathlib import Path
import sys

import mcp_gate
from receipt_layout import receipt_root


def check(engine_config):
    from mindie_knowledge.state_layout import state_root
    config = json.loads(Path(engine_config).read_text(encoding='utf-8'))
    state_root(config['root'], config['domain'])
    receipt_root(mcp_gate.remote_state_dir())
    return dict(status='compatible')


if __name__ == '__main__':
    print(json.dumps(check(sys.argv[1])))
