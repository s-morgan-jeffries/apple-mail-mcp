#!/bin/bash
#
# Apple Mail CLI Installer
# Installs the apple-mail command-line tool
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
NC="\033[0m" # No Color

echo -e "${BOLD}Apple Mail CLI Installer${NC}"
echo "========================="
echo ""

# Check if running on macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo -e "${RED}Error: This tool only works on macOS${NC}"
    exit 1
fi

# Check Python version
check_python() {
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        MAJOR_VERSION=$(echo "$PYTHON_VERSION" | cut -d. -f1)
        MINOR_VERSION=$(echo "$PYTHON_VERSION" | cut -d. -f2)
        
        if [[ "$MAJOR_VERSION" -ge 3 ]] && [[ "$MINOR_VERSION" -ge 10 ]]; then
            echo -e "${GREEN}✓${NC} Python $PYTHON_VERSION found"
            return 0
        fi
    fi
    
    echo -e "${RED}✗${NC} Python 3.10+ is required"
    echo "  Install Python 3.10+ from https://python.org or via Homebrew:"
    echo "  brew install python@3.12"
    exit 1
}

# Check if pip is available
check_pip() {
    if python3 -m pip --version &> /dev/null; then
        echo -e "${GREEN}✓${NC} pip is available"
        return 0
    fi
    
    echo -e "${RED}✗${NC} pip is not available"
    echo "  Install pip: python3 -m ensurepip --upgrade"
    exit 1
}

# Install options
install_options() {
    echo ""
    echo "Installation Options:"
    echo "  1) Install in user space (recommended)"
    echo "  2) Install in virtual environment"
    echo "  3) Install for development (editable)"
    echo ""
    read -p "Choose an option [1]: " choice
    choice=${choice:-1}
    
    case $choice in
        1)
            install_user
            ;;
        2)
            install_venv
            ;;
        3)
            install_dev
            ;;
        *)
            echo -e "${RED}Invalid option${NC}"
            exit 1
            ;;
    esac
}

# Install in user space
install_user() {
    echo ""
    echo -e "${BOLD}Installing in user space...${NC}"
    cd "$SCRIPT_DIR"
    
    python3 -m pip install --user . --quiet
    
    # Check if user bin is in PATH
    USER_BIN="$HOME/.local/bin"
    if [[ ":$PATH:" != *":$USER_BIN:"* ]]; then
        echo ""
        echo -e "${YELLOW}⚠${NC}  Add the following to your shell profile (.zshrc or .bashrc):"
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo ""
        echo "Then restart your terminal or run: source ~/.zshrc"
    fi
    
    install_complete
}

# Install in virtual environment
install_venv() {
    echo ""
    read -p "Virtual environment path [./venv]: " VENV_PATH
    VENV_PATH=${VENV_PATH:-"$SCRIPT_DIR/venv"}
    
    echo -e "${BOLD}Creating virtual environment at $VENV_PATH...${NC}"
    python3 -m venv "$VENV_PATH"
    
    echo -e "${BOLD}Installing apple-mail CLI...${NC}"
    "$VENV_PATH/bin/pip" install "$SCRIPT_DIR" --quiet
    
    echo ""
    echo -e "${GREEN}✓${NC} Installed in virtual environment"
    echo ""
    echo "To use the CLI, either:"
    echo "  1. Activate the venv: source $VENV_PATH/bin/activate"
    echo "  2. Use the full path: $VENV_PATH/bin/apple-mail"
    echo ""
    echo "To create a symlink in /usr/local/bin:"
    echo "  sudo ln -sf $VENV_PATH/bin/apple-mail /usr/local/bin/apple-mail"
    
    install_complete_venv "$VENV_PATH"
}

# Install for development (editable)
install_dev() {
    echo ""
    echo -e "${BOLD}Installing in development mode...${NC}"
    cd "$SCRIPT_DIR"
    
    # Check if in a virtual environment
    if [[ -z "$VIRTUAL_ENV" ]]; then
        echo -e "${YELLOW}⚠${NC}  Not in a virtual environment. Creating one..."
        python3 -m venv "$SCRIPT_DIR/.venv"
        source "$SCRIPT_DIR/.venv/bin/activate"
        echo -e "${GREEN}✓${NC} Virtual environment created and activated at .venv"
    fi
    
    pip install -e ".[dev]" --quiet
    
    echo ""
    echo -e "${GREEN}✓${NC} Installed in development mode"
    echo ""
    echo "The CLI is now available as 'apple-mail' when the venv is active."
    echo "Changes to the source code will be reflected immediately."
    
    install_complete_dev
}

install_complete() {
    echo ""
    echo -e "${GREEN}✓ Installation complete!${NC}"
    echo ""
    echo -e "${BOLD}Usage:${NC}"
    echo "  apple-mail --help                    Show all commands"
    echo "  apple-mail list-mailboxes Gmail      List mailboxes"
    echo "  apple-mail search Gmail --limit 10   Search messages"
    echo "  apple-mail get <message_id>          Get message details"
    echo ""
    echo "For more information, see the documentation at:"
    echo "  https://github.com/morgancoopercom/apple-mail-mcp"
}

install_complete_venv() {
    local venv_path=$1
    echo ""
    echo -e "${GREEN}✓ Installation complete!${NC}"
    echo ""
    echo -e "${BOLD}Quick start:${NC}"
    echo "  source $venv_path/bin/activate"
    echo "  apple-mail --help"
}

install_complete_dev() {
    echo ""
    echo -e "${GREEN}✓ Development installation complete!${NC}"
    echo ""
    echo -e "${BOLD}Quick start:${NC}"
    echo "  apple-mail --help"
    echo ""
    echo "Run tests with:"
    echo "  pytest"
}

# Main
echo "Checking requirements..."
echo ""
check_python
check_pip

install_options

