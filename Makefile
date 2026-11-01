PYTHON ?= python3
PREFIX ?= /usr/local
RULE   := udev/99-hiska-keg450.rules

.PHONY: help check info testcard install-udev service

help:
	@echo "make info        - show the detected hidraw nodes"
	@echo "make testcard    - upload the 4-colour test card"
	@echo "make check       - byte-compile and smoke-test the scripts"
	@echo "make install-udev- install the udev rule (needs sudo)"
	@echo "make service     - install and enable the systemd user unit (live clock)"

info:
	./keg450.py info

testcard:
	./keg450.py testcard

check:
	$(PYTHON) -m py_compile keg450.py live.py
	./keg450.py --version
	./keg450.py light --mode list
	./keg450.py settings --dry-run

install-udev:
	sudo install -m644 $(RULE) /etc/udev/rules.d/
	sudo udevadm control --reload-rules
	sudo udevadm trigger --subsystem-match=hidraw

service:
	mkdir -p $(HOME)/.config/systemd/user
	cp contrib/systemd/hiska-keg450-live.service $(HOME)/.config/systemd/user/
	systemctl --user daemon-reload
	systemctl --user enable --now hiska-keg450-live.service
