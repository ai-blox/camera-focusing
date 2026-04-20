#!/bin/bash
lp5815_path="/sys/bus/i2c/drivers/lp5815/1-002d"
chip_setup_path="$lp5815_path/lp5815_chip_setup"

# 1. Wake up the chip identically to the working script
echo 1 > "$chip_setup_path/device_enable"
echo 1 > "$chip_setup_path/charging_mode"

# 2. Set to Direct Mode
for LED in LED0 LED1 LED2; do
    echo 1 > "$lp5815_path/$LED/led_enable"
    echo direct > "$lp5815_path/$LED/led_mode"
done

# 3. Start the chip state machine
echo start > "$chip_setup_path/device_command"

# 4. Helper function to set R, G, B current
update_colors() {
    echo $1 > "$lp5815_path/LED0/dot_current"
    echo $2 > "$lp5815_path/LED1/dot_current"
    echo $3 > "$lp5815_path/LED2/dot_current"
    sleep 0.05
}

# 5. Rainbow Phase Loop (Capped at 150 to match your brightness)
while true; do
    for ((i=0; i<=150; i+=10)); do update_colors 150 $i 0; done
    for ((i=150; i>=0; i-=10)); do update_colors $i 150 0; done
    for ((i=0; i<=150; i+=10)); do update_colors 0 150 $i; done
    for ((i=150; i>=0; i-=10)); do update_colors 0 $i 150; done
    for ((i=0; i<=150; i+=10)); do update_colors $i 0 150; done
    for ((i=150; i>=0; i-=10)); do update_colors 150 0 $i; done
done
