import { Controller, Get, Post, Body } from '@nestjs/common';

@Controller('orders')
export class OrdersController {
  @Get()
  findAll() {
    return [];
  }

  @Get(':id')
  findOne(@Body() id: string) {
    return { id };
  }

  @Post()
  create(@Body() dto: unknown) {
    return { created: true };
  }
}
