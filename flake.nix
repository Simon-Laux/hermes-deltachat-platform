{
  description = "Delta Chat platform plugin for Hermes Agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            # Python
            python3
            python3Packages.pytest
            python3Packages.pytest-asyncio
            python3Packages.black
            python3Packages.flake8

            # Tools
            jq

            # Delta Chat
            deltachat-rpc-server
            # Python package for development/testing
            (python3.withPackages (ps: with ps; [ deltachat2 aiortc numpy ]))
          ];

          shellHook = ''
            export PYTHONPATH=$PYTHONPATH:.
          '';
        };

        # Package for installation - installs plugin to Hermes plugins directory
        packages.default = pkgs.stdenv.mkDerivation {
          name = "deltachat-platform";
          src = ./.;
          buildPhase = ''
            echo "Building Delta Chat platform plugin..."
          '';
          installPhase = ''
            mkdir -p $out/share/hermes/plugins/deltachat-platform
            cp -r adapter.py __init__.py plugin.yaml README.md LICENSE docs/ skills/ setup.py vendor/ $out/share/hermes/plugins/deltachat-platform/
          '';
        };
      }
    );
}
