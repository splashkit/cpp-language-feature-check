#include "splashkit.h"

int main()
{
    open_window("House Drawing", 800, 600);

    clear_screen(COLOR_WHITE);
    fill_ellipse(COLOR_BRIGHT_GREEN, 0, 400, 800, 400);
    fill_rectangle(COLOR_GRAY, 300 * 1, 300, 200, 200);
    fill_triangle(COLOR_RED, 250 + 20, 300, 400, 150, 550, 300);

    refresh_screen();

    delay(5000);
}
