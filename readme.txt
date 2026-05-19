apt install python3-pip git mpg123 ffmpeg python3-numpy python3-pygame python3-flask python3-pil python3-numpy python3-spidev
pip3 install st7789 --break-system-packages

sudo raspi-config nonint do_spi 0

vi /boot/firmware/config.txt
# Enable audio (loads snd_bcm2835)
#dtparam=audio=on

# CDMAN for pirate audio dac
dtparam=audio=off
dtoverlay=hifiberry-dac
gpio=25=op,dh

======================================================


cd ~/cdman
chmod +x cdman-player

# enable SPI for the display
sudo raspi-config nonint do_spi 0

# install display libs
sudo apt install -y python3-pil python3-numpy python3-spidev
pip3 install st7789 --break-system-packages

# (re)start the web server
sudo systemctl restart cdman-web.service   # if already installed as a service
# or: python3 cdman-web.py

# install + start the display daemon
sudo cp cdman-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cdman-display.service
sudo systemctl status cdman-display.service










======================


Then later use Furnace Tracker

After you understand the sound style, use Furnace Tracker for serious chiptune composing. It supports many old sound chips and can export WAV/VGM files.

Good chips to try in Furnace:

Style	Chip
Sega Genesis	YM2612
Commodore 64	SID
DOS AdLib	OPL2/OPL3
NES/GameBoy feel	NES/GameBoy chips


NOTE_A3,NOTE_C4,NOTE_E3,NOTE_B3,
NOTE_C3,NOTE_A3,NOTE_E3,NOTE_B3,
NOTE_D3,NOTE_A3,NOTE_B3,NOTE_F4,
NOTE_E3,NOTE_GS3,NOTE_B3,NOTE_D4,
NOTE_CS3,NOTE_CS4,NOTE_GS3,NOTE_B3,
NOTE_FS3,NOTE_A3,NOTE_D3,NOTE_FS4,
NOTE_E3,NOTE_A3,NOTE_B3,NOTE_E4,
NOTE_E3,NOTE_GS3,NOTE_E4,NOTE_D4,
NOTE_CS4,NOTE_A3,NOTE_D4,NOTE_B3,
NOTE_E4,NOTE_CS4,NOTE_A3,NOTE_E3,
NOTE_CS3,NOTE_F3,NOTE_A3,NOTE_CS4,
NOTE_B3,NOTE_GS3,NOTE_F3,NOTE_B2,
NOTE_A2,NOTE_CS3,NOTE_FS3,NOTE_A3,
NOTE_D3,NOTE_FS3,NOTE_B3,NOTE_D4,
NOTE_E3,NOTE_A3,NOTE_D4,NOTE_CS4,
NOTE_E3,NOTE_GS3,NOTE_C4,NOTE_B3