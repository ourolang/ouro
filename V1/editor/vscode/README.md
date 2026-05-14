# Ouro for VS Code

Syntax highlighting for `.ou` files (the [Ouro](https://github.com/ourolang/ouro)
programming language).

## Features

- Keywords, control flow, declarations
- Built-in primitive types (`i32`, `usize`, `f64`, `bool`, …)
- String, byte, and numeric literals (including hex / binary / octal, suffixes,
  and digit separators)
- Triple-quoted multi-line strings
- Comments (`#` to end of line)
- Dunder methods (`__drop__`, `__iter__`, …)
- Wrapper-only modifiers: `var`, `const`, `weak`, `ptr`
- Type-test operator `?=` and union types `T1 | T2`
- Indentation-aware editing

## Install (local development)

The extension is not yet published to the Marketplace. Pick one of the
following methods:

### Option A — symlink into the VS Code extensions directory (simplest)

```
ln -s "$PWD" ~/.vscode/extensions/ourolang.ouro-0.1.0
```

Then restart VS Code (or run "Developer: Reload Window" from the command
palette). VS Code only picks up new extensions on start, so a reload is
required.

### Option B — package and install a `.vsix`

Requires [`vsce`](https://github.com/microsoft/vscode-vsce):

```
npm install -g @vscode/vsce
cd editor/vscode
vsce package
code --install-extension ouro-0.1.0.vsix
```

### Option C — Extension Development Host (live reload, for grammar work)

Open `editor/vscode/` in VS Code and press `F5`. A second VS Code window
opens with the extension loaded; edits to the grammar reload on save.
