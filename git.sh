#!/bin/sh
dir=$(dirname "$(realpath $0)")
cd $dir
git config --global user.email "you@example.com"
git config --global user.name "Your Name"
git add --all
git add .
git commit -n
git push ssh://git@github.com/definitly486/flatpak-freebsd