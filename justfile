# 根文件只负责统一导入和命令发现，具体职责按领域拆分到 justfiles/。
set dotenv-load
set positional-arguments

import 'justfiles/dev.just'
import 'justfiles/test.just'
import 'justfiles/db.just'
import 'justfiles/docker.just'
import 'justfiles/ops.just'

default:
    @just --list --list-heading 'Available recipes:\n'
