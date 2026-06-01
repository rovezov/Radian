#compdef radian radian-backup radian-calendar radian-contacts radian-cookbook radian-docs radian-gallery radian-mail radian-mcp radian-memory radian-notes radian-personal radian-preset radian-research radian-sessions radian-signature radian-skills radian-tasks radian-theme radian-webhook
# Zsh tab-completion for the radian umbrella + sub-CLIs.
#
# Drop in any directory on $fpath, e.g.:
#     fpath=(/path/to/radian-ui/scripts/_completion $fpath)
#     autoload -U compinit; compinit
#
# Then `radian <tab>` completes subcommands; `radian mail <tab>`
# completes mail subcommands; `radian-mail <tab>` works the same.

_radian_scripts_dir() {
    local self="${(%):-%x}"
    while [[ -L "$self" ]]; do self="$(readlink "$self")"; done
    cd "${self:h}/.." && pwd
}

typeset -gA _radian_subs

_radian_refresh() {
    _radian_subs=()
    local dir="$(_radian_scripts_dir)"
    local py="$dir/../venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local f sub help_out commands
    for f in "$dir"/radian-*; do
        [[ -x "$f" ]] || continue
        case "$f" in
            *.bak|*.pyc|*.pre-*) continue ;;
        esac
        sub="${${f:t}#radian-}"
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _radian_subs[$sub]="$commands"
    done
}

_radian() {
    [[ ${#_radian_subs} -eq 0 ]] && _radian_refresh

    local cmd="${words[1]}"

    if [[ "$cmd" == "radian" ]]; then
        if (( CURRENT == 2 )); then
            local -a subs=(${(k)_radian_subs} help)
            _describe 'subcommand' subs
            return
        fi
        local sub="${words[2]}"
        if [[ "$sub" == "help" ]] && (( CURRENT == 3 )); then
            local -a subs=(${(k)_radian_subs})
            _describe 'subcommand' subs
            return
        fi
        if (( CURRENT == 3 )); then
            local -a sc=(${(s/ /)_radian_subs[$sub]})
            _describe 'command' sc
            return
        fi
        return
    fi

    # radian-foo <tab>
    local sub="${cmd#radian-}"
    if (( CURRENT == 2 )); then
        local -a sc=(${(s/ /)_radian_subs[$sub]})
        _describe 'command' sc
        return
    fi
}

_radian "$@"
