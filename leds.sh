#!/bin/sh
lp5815_path="/sys/bus/i2c/drivers/lp5815/1-002d"
chip_setup_path="$lp5815_path/lp5815_chip_setup"
chip_enable="$chip_setup_path/device_enable"

autonomousSetup() {
	local led_path=$1
	local autonomous_dir="$led_path/autonomous"
	local Engine0_path="$autonomous_dir/Engine0"
	local order_path="$Engine0_path/Engine0_Order0"
	local pattern_path="$order_path/PATTERN0"

	echo 1 > "$Engine0_path/engine_enable"
	echo 1 > "$order_path/order_enable"
	echo 1 > "$pattern_path/pattern_enable"
	echo 3 > "$Engine0_path/engine_repeat_time"
	echo 15 > "$pattern_path/repeat_time"
	echo 9:9 > "$pattern_path/pause_time"
	echo 11:11 > "$pattern_path/sloper_time1"
	echo 11:11 > "$pattern_path/sloper_time2"
	echo 170 > "$pattern_path/pwm0"
	echo 100 > "$pattern_path/pwm1"
	echo 20 > "$pattern_path/pwm2"
	echo 100 > "$pattern_path/pwm3"
	echo 170 > "$pattern_path/pwm4"
}

led=$1
stat=$2
led_path="$lp5815_path/$led"

echo 1 > "$chip_enable"
echo 1 > "$chip_setup_path/charging_mode"

if [ "$stat" = "start" ]; then
	echo 1 > "$led_path/led_enable"
	echo autonomous > "$led_path/led_mode"
	echo 150 > "$led_path/dot_current"
	autonomousSetup "$led_path"
	echo start > "$chip_setup_path/device_command"
elif [ "$stat" = "stop" ]; then
	echo stop > "$chip_setup_path/device_command"
	echo 0 > "$led_path/led_enable"
fi
