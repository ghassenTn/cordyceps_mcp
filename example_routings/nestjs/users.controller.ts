// NestJS: @Controller prefix + @Get/@Post method decorators
import { Controller, Get, Post, Param } from '@nestjs/common';

@Controller('users')
export class UsersController {
  @Get()
  findAll() {
    return [];
  }

  @Get(':id')
  findOne(@Param('id') id: string) {
    return { id };
  }

  @Post()
  create() {
    return { created: true };
  }
}
